"""Unit tests for the pure half of workflows/pr_cross_review.py.

    python3 -m unittest discover -s tests

Standard library only, no container and no network: everything here is a function of
plain data. That is the half of the workflow where a mistake is invisible -- a wrong
path or a wrongly merged finding does not raise, it produces a confident review of the
wrong thing after fifteen minutes and three live model sessions.
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
import warnings

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "workflows", "pr_cross_review.py")


def _load(repo="owner/name", pr=1, run_id="test-run"):
    """Load the workflow module for its helpers. Returns (module, exit code).

    Two things stand between the file and an import: it pulls `cao_workflow`, which
    exists only inside the container, and it reads its inputs at import time. Both are
    stubbed here. The script then stops itself with SystemExit(0) at the line where the
    network and the filesystem begin, so what comes back is the namespace built up to
    that point -- a bad `repo` exits earlier, with the message as the code.
    """
    shim = types.ModuleType("cao_workflow")
    shim.ShimError = type("ShimError", (Exception,), {})
    shim.ShimHTTPError = type("ShimHTTPError", (shim.ShimError,), {})
    shim.get_inputs = lambda: {"repo": repo, "pr": pr}
    shim.emit_output = lambda payload: None
    shim.step = lambda *args, **kwargs: None
    saved = sys.modules.get("cao_workflow")
    sys.modules["cao_workflow"] = shim
    was = os.environ.get("CAO_WORKFLOW_RUN_ID")
    if run_id is None:
        os.environ.pop("CAO_WORKFLOW_RUN_ID", None)
    else:
        os.environ["CAO_WORKFLOW_RUN_ID"] = run_id
    try:
        spec = importlib.util.spec_from_file_location("pr_cross_review", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        code = None
        try:
            spec.loader.exec_module(module)
        except SystemExit as exc:
            code = exc.code
        return module, code
    finally:
        if saved is None:
            sys.modules.pop("cao_workflow", None)
        else:
            sys.modules["cao_workflow"] = saved
        if was is None:
            os.environ.pop("CAO_WORKFLOW_RUN_ID", None)
        else:
            os.environ["CAO_WORKFLOW_RUN_ID"] = was


MOD, _EXIT = _load()


def setUpModule():
    """The workflow reads files with the one-liner `open(path).read()` idiom throughout.

    CPython closes those when the refcount drops and warns while doing it, which would
    bury this output in ResourceWarnings that say nothing about the code under test.
    Filtered here rather than at import: unittest resets the filters when a run starts.
    """
    warnings.filterwarnings("ignore", category=ResourceWarning)


def _finding(source, index, file="a.py", line=10, category="correctness"):
    """One normalized finding, the way _norm would have produced it."""
    return MOD._norm({"file": file, "line": line, "category": category,
                      "severity": "high", "title": "t", "detail": "d"}, source, index)


class ImportGuard(unittest.TestCase):
    def test_stops_before_stage_0(self):
        self.assertEqual(_EXIT, 0)
        self.assertTrue(hasattr(MOD, "_dedup"))
        # Stage 0 defines this one; reaching it would have run git and gh.
        self.assertFalse(hasattr(MOD, "PR_REF"))


class RepoSpec(unittest.TestCase):
    def test_owner_name(self):
        module, _ = _load("owner/name")
        self.assertEqual((module.OWNER, module.NAME, module.SLUG),
                         ("owner", "name", "owner__name"))

    def test_https_url(self):
        module, _ = _load("https://github.com/owner/name/")
        self.assertEqual((module.OWNER, module.NAME), ("owner", "name"))

    def test_pasted_pr_url(self):
        module, _ = _load("https://github.com/owner/name/pull/12/files")
        self.assertEqual((module.OWNER, module.NAME), ("owner", "name"))

    def test_scp_style_remote(self):
        module, _ = _load("git@github.com:owner/name.git")
        self.assertEqual((module.OWNER, module.NAME), ("owner", "name"))

    def test_garbage_exits_with_a_message(self):
        _, code = _load("nonsense")
        self.assertIsInstance(code, str)
        self.assertIn("owner/name", code)


class Norm(unittest.TestCase):
    def test_dotfile_path_survives(self):
        # str.lstrip("./") ate the leading dot: the judge then opened a path that does
        # not exist and returned needs-context on a real defect.
        self.assertEqual(_finding("claude", 1, file=".dockerignore")["file"],
                         ".dockerignore")
        self.assertEqual(_finding("claude", 1, file=".github/workflows/ci.yml")["file"],
                         ".github/workflows/ci.yml")

    def test_leading_dot_slash_is_stripped(self):
        self.assertEqual(_finding("claude", 1, file="./src/a.py")["file"], "src/a.py")

    def test_absolute_path_is_made_relative_to_the_checkout(self):
        absolute = os.path.join(MOD.REPO, "src", "a.py")
        self.assertEqual(_finding("claude", 1, file=absolute)["file"], "src/a.py")

    def test_unknown_severity_and_category_fall_back(self):
        out = MOD._norm({"severity": "blocker", "category": "style"}, "codex", 3)
        self.assertEqual(out["severity"], "medium")
        self.assertEqual(out["category"], "correctness")
        self.assertEqual(out["id"], "codex-3")
        self.assertEqual(out["sources"], ["codex"])

    def test_unusable_line_becomes_none(self):
        self.assertIsNone(MOD._norm({"line": "somewhere"}, "claude", 1)["line"])
        self.assertEqual(MOD._norm({"line": "42"}, "claude", 1)["line"], 42)


class ReadJson(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, text):
        path = os.path.join(self.dir, "out.json")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_code_fences_and_prose(self):
        path = self._write('Here you go:\n```json\n{"verdicts": [1]}\n```\n')
        self.assertEqual(MOD._read_json(path, None), {"verdicts": [1]})

    def test_unparseable_returns_the_default(self):
        self.assertEqual(MOD._read_json(self._write("sorry, I could not"), "d"), "d")

    def test_missing_file_returns_the_default(self):
        self.assertIsNone(MOD._read_json(os.path.join(self.dir, "nope.json"), None))

    def test_top_level_array_does_not_raise(self):
        # The crash this guards: an array is truthy, so `(x or {}).get(...)` reached
        # list.get and the AttributeError killed the run after round 1 had been paid for.
        path = self._write('[{"id": "f-01", "verdict": "confirmed"}]')
        self.assertEqual(MOD._read_items(path, "verdicts"),
                         [{"id": "f-01", "verdict": "confirmed"}])

    def test_an_empty_list_is_an_answer_and_nothing_written_is_not(self):
        # The merge step's non-delivery hid here: {"merges": []} means "no duplicates",
        # a missing file means the step wrote nothing, and both used to come back [].
        self.assertEqual(MOD._read_items(self._write('{"merges": []}'), "merges"), [])
        self.assertIsNone(MOD._read_items(os.path.join(self.dir, "nope.json"), "merges"))

    def test_null_under_the_key_is_nothing(self):
        self.assertIsNone(MOD._read_items(self._write('{"verdicts": null}'), "verdicts"))

    def test_object_under_the_key_is_nothing(self):
        self.assertIsNone(MOD._read_items(self._write('{"merges": {"a": 1}}'), "merges"))


class LoadRound1(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write(self, text):
        path = os.path.join(self.dir, "claude.json")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_nothing_written_is_none_not_empty(self):
        self.assertIsNone(MOD._load_round1(os.path.join(self.dir, "nope.json"), "claude"))

    def test_reviewed_and_found_nothing_is_an_empty_list(self):
        self.assertEqual(MOD._load_round1(self._write('{"findings": []}'), "claude"), [])

    def test_bare_array_is_accepted(self):
        found = MOD._load_round1(self._write('[{"file": "a.py", "line": 3}]'), "claude")
        self.assertEqual([f["id"] for f in found], ["claude-1"])

    def test_null_findings_is_a_failure(self):
        self.assertIsNone(MOD._load_round1(self._write('{"findings": null}'), "claude"))


class Dedup(unittest.TestCase):
    def test_same_place_from_two_harnesses_is_one_corroborated_finding(self):
        out = MOD._dedup([_finding("claude", 1), _finding("codex", 1)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sources"], ["claude", "codex"])
        self.assertEqual(out[0]["independent_sources"], 2)
        self.assertTrue(out[0]["corroborated"])
        self.assertEqual(out[0]["merged_ids"], ["codex-1"])

    def test_a_collapse_keeps_the_write_up_it_folds(self):
        # Same file, same line, same category is where two harnesses agree; whether they
        # describe one defect is a judgement. This collapse skips round 2, so the loser's
        # words are the only way anyone downstream can check it -- and they were dropped.
        other = _finding("codex", 1)
        other["title"] = "a different defect at the same line"
        out = MOD._dedup([_finding("claude", 1), other])
        self.assertEqual([f["id"] for f in out], ["claude-1"])
        self.assertTrue(out[0]["corroborated"])
        self.assertEqual([m["title"] for m in out[0]["merged_from"]],
                         ["a different defect at the same line"])

    def test_nearby_lines_are_different_findings(self):
        # A missing bounds check at :100 and a wrong return value at :102 are two bugs.
        # Proximity used to collapse them and mark the survivor corroborated, which
        # also skipped the jury -- one auto-confirmed claim out of two real ones.
        out = MOD._dedup([_finding("claude", 1, line=100), _finding("codex", 1, line=102)])
        self.assertEqual(len(out), 2)
        self.assertEqual([f["corroborated"] for f in out], [False, False])

    def test_same_line_different_category_stays_apart(self):
        out = MOD._dedup([_finding("claude", 1),
                          _finding("codex", 1, category="security")])
        self.assertEqual(len(out), 2)

    def test_findings_without_a_line_are_never_collapsed(self):
        out = MOD._dedup([_finding("claude", 1, line=None), _finding("codex", 1, line=None)])
        self.assertEqual(len(out), 2)

    def test_one_harness_filing_twice_at_one_line_keeps_both(self):
        # A missing bounds check and an ignored error can share a line and a category.
        # Collapsing them deleted the second from the run outright: this path builds no
        # `merged_from`, so nothing downstream ever saw its words.
        out = MOD._dedup([_finding("claude", 1), _finding("claude", 2)])
        self.assertEqual([f["id"] for f in out], ["claude-1", "claude-2"])
        self.assertEqual(out[0]["independent_sources"], 1)

    def test_one_harness_alone_is_not_corroborated(self):
        out = MOD._dedup([_finding("claude", 1)])
        self.assertFalse(out[0]["corroborated"])
        self.assertEqual(out[0]["cluster"], "")


class MarkClusters(unittest.TestCase):
    """Nothing is folded away any more: a merge is a label, and labels lose nothing."""

    def _three(self):
        return MOD._dedup([_finding("claude", 1, line=10),
                           _finding("codex", 1, line=200),
                           _finding("opencode", 1, line=400)])

    def test_a_group_labels_its_members_and_keeps_them_all(self):
        out = MOD._mark_clusters(self._three(), [["claude-1", "codex-1"]])
        self.assertEqual([f["id"] for f in out], ["claude-1", "codex-1", "opencode-1"])
        self.assertEqual(out[0]["cluster"], out[1]["cluster"])
        self.assertTrue(out[0]["cluster"])
        self.assertEqual(out[2]["cluster"], "")
        # Both keep their author, so both still have two judges -- which folding cost
        # them: a grouped finding used to reach the arbiter with no verdict at all.
        self.assertEqual(len(MOD._jury_targets(out, "opencode")), 2)
        self.assertEqual([f["id"] for f in MOD._jury_targets(out, "claude")],
                         ["codex-1", "opencode-1"])

    def test_the_order_of_a_group_changes_nothing(self):
        forward = MOD._mark_clusters(self._three(), [["claude-1", "codex-1"]])
        backward = MOD._mark_clusters(self._three(), [["codex-1", "claude-1"]])
        self.assertEqual([(f["id"], f["cluster"]) for f in forward],
                         [(f["id"], f["cluster"]) for f in backward])

    def test_corroboration_is_untouched_by_a_merge(self):
        # Two harnesses at one line, a third describing it elsewhere. Folding this lost
        # the corroboration, or handed it to the wrong claim, depending on the round.
        deduped = MOD._dedup([_finding("claude", 1, line=10), _finding("codex", 1, line=10),
                              _finding("opencode", 1, line=400)])
        out = MOD._mark_clusters(deduped, [["opencode-1", "claude-1"]])
        corroborated = [f for f in out if f["corroborated"]]
        self.assertEqual([f["id"] for f in corroborated], ["claude-1"])
        self.assertEqual(corroborated[0]["independent_sources"], 2)
        self.assertEqual(out[0]["cluster"], out[1]["cluster"])

    def test_overlapping_groups_make_one_cluster(self):
        # ["claude-1","codex-1"] then ["opencode-1","codex-1"]: the model said all three
        # are one defect. The second group used to collapse to one member and vanish,
        # leaving the duplicate the merge stage exists to remove.
        out = MOD._mark_clusters(self._three(), [["claude-1", "codex-1"],
                                                 ["opencode-1", "codex-1"]])
        self.assertEqual(len({f["cluster"] for f in out}), 1)
        self.assertTrue(all(f["cluster"] for f in out))

    def test_a_group_that_would_pair_one_reviewer_with_itself_is_left_out(self):
        deduped = MOD._dedup([_finding("claude", 1, line=10),
                              _finding("claude", 2, line=200)])
        out = MOD._mark_clusters(deduped, [["claude-1", "claude-2"]])
        self.assertEqual([f["cluster"] for f in out], ["", ""])

    def test_a_union_that_would_do_the_same_is_left_out_too(self):
        # A==B and B==C is one cluster; A==B and B==A2 would put Claude's two findings
        # in one, which the merge prompt forbids and the union check catches.
        deduped = MOD._dedup([_finding("claude", 1, line=10), _finding("codex", 1, line=200),
                              _finding("claude", 2, line=400)])
        out = MOD._mark_clusters(deduped, [["claude-1", "codex-1"],
                                           ["codex-1", "claude-2"]])
        self.assertEqual([f["cluster"] for f in out[:2]], [out[0]["cluster"]] * 2)
        self.assertTrue(out[0]["cluster"])
        self.assertEqual(out[2]["cluster"], "")

    def test_junk_groups_neither_crash_nor_label(self):
        out = MOD._mark_clusters(self._three(), [[["claude-1", "codex-1"]],
                                                 ["claude-1", ["codex-1"]],
                                                 "claude-1", [], ["claude-1"],
                                                 ["claude-1", "codex-99"]])
        self.assertEqual([f["cluster"] for f in out], ["", "", ""])

    def test_clusters_are_json_serialisable(self):
        out = MOD._mark_clusters(self._three(), [["claude-1", "codex-1"]])
        self.assertIn('"cluster": "c1"', json.dumps(out))


class StageZero(unittest.TestCase):
    """The half that destroys state rather than raising when it is wrong."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_a_first_execution_reads_the_pr_and_rebuilds_the_checkout(self):
        # No snapshot: nothing pinned, so the PR is read as it is now and the worktree is
        # never accepted as found -- the last review's copy may have been written into.
        self.assertEqual(MOD._resume_plan({}, False, False, ""), (False, False))
        self.assertEqual(MOD._resume_plan({}, True, True, "abc"), (False, False))

    def test_a_resume_keeps_its_pin_and_accepts_its_own_checkout(self):
        pinned = {"run_id": "run-1", "head": "abc"}
        self.assertEqual(MOD._resume_plan(pinned, True, True, "abc"), (True, True))

    def test_a_resume_whose_checkout_moved_or_went_keeps_its_pin_anyway(self):
        # This is what used to make a resume look like a fresh run, which is how it came
        # to delete its own report: the answer is rebuild at the pin, not re-read.
        pinned = {"run_id": "run-1", "head": "abc"}
        self.assertEqual(MOD._resume_plan(pinned, True, True, "def"), (True, False))
        self.assertEqual(MOD._resume_plan(pinned, True, True, ""), (True, False))

    def test_a_snapshot_without_its_diff_is_not_a_resume(self):
        pinned = {"run_id": "run-1", "head": "abc"}
        self.assertEqual(MOD._resume_plan(pinned, False, True, "abc"), (False, False))
        self.assertEqual(MOD._resume_plan({"run_id": "run-1"}, True, True, "abc"),
                         (False, False))

    def test_a_lock_is_held_until_it_is_released(self):
        path = os.path.join(self.dir, "locks", "owner__name.pr-1.lock")
        first = MOD._take_lock(path)
        self.assertIsNotNone(first)
        self.assertIsNone(MOD._take_lock(path))
        first.close()
        second = MOD._take_lock(path)
        self.assertIsNotNone(second)
        second.close()

    def test_the_empty_report_names_who_delivered_and_who_did_not(self):
        text = MOD._empty_report(7, ["claude"], {"codex": "no usable findings file"})
        self.assertIn("# PR #7 cross-review", text)
        self.assertIn("No findings: claude reviewed", text)
        self.assertIn("Did not deliver", text)
        self.assertIn("`codex` - no usable findings file", text)

    def test_the_empty_report_says_nothing_of_failures_when_there_are_none(self):
        text = MOD._empty_report(7, ["claude", "codex", "opencode"], {})
        self.assertIn("claude, codex, opencode reviewed", text)
        self.assertNotIn("Did not deliver", text)

    def test_a_report_has_to_exist_and_have_something_in_it(self):
        missing = os.path.join(self.dir, "final-review.md")
        self.assertFalse(MOD._report_is_usable(missing))
        open(missing, "w").write("")
        self.assertFalse(MOD._report_is_usable(missing))
        open(missing, "w").write("# PR #1 cross-review\n")
        self.assertFalse(MOD._report_is_usable(missing))

    def test_a_short_report_is_still_a_report(self):
        # The 200-byte floor failed this one: when the jury rejects every finding the
        # honest review is a heading and a sentence, and the run was recorded FAILED
        # with its structured output dropped.
        path = os.path.join(self.dir, "final-review.md")
        open(path, "w").write("# PR #123 cross-review\n\nNo actionable defects remain; "
                              "both judges rejected the sole finding.\n")
        self.assertLess(os.path.getsize(path), 200)
        self.assertTrue(MOD._report_is_usable(path))

    def test_the_exit_note_carries_what_the_dropped_output_would_have(self):
        # A non-zero exit makes CAO drop emit_output's sentinel, so stderr is the only
        # place left to say which stage went missing and where the report is.
        final = os.path.join(self.dir, "final-review.md")
        open(final, "w").write("# report\n\nbody\n")
        note = MOD._exit_note(1, final, {"claude": "no usable findings file"})
        self.assertIn('"claude": "no usable findings file"', note)
        self.assertIn("final review: %s" % final, note)
        # A run that succeeded says nothing at all.
        self.assertEqual(MOD._exit_note(0, final, {"claude": "x"}), "")
        # And a run with no report does not name one.
        note = MOD._exit_note(1, os.path.join(self.dir, "nope.md"), {"arbiter": "silent"})
        self.assertIn("arbiter", note)
        self.assertNotIn("final review:", note)

    def test_a_held_lock_keeps_a_siblings_checkout(self):
        # Inverting this is the one regression that destroys work in progress: the
        # worktree three agents are reading gets removed out from under them.
        entries = ["pr-1", "pr-2", "pr-7", "notes.txt"]
        taken = []

        def take(entry):
            if entry == "pr-7":
                return None          # another run is reviewing pr-7 right now
            handle = type("H", (), {"close": lambda self: taken.append(entry)})()
            return handle

        drop = MOD._worktrees_to_drop(entries, "pr-2", lambda e: True, take)
        self.assertEqual([entry for entry, _ in drop], ["pr-1"])
        # The lock comes back HELD -- the removal has to happen inside it.
        self.assertEqual(taken, [])

    def test_only_reviewed_siblings_are_dropped(self):
        entries = ["pr-1", "pr-2", "pr-3"]
        take = lambda e: type("H", (), {"close": lambda self: None})()
        drop = MOD._worktrees_to_drop(entries, "pr-2", lambda e: e == "pr-3", take)
        self.assertEqual([entry for entry, _ in drop], ["pr-3"])

    def test_a_moved_head_is_refused_by_name(self):
        meta = json.dumps({"headRefOid": "b" * 40, "title": "t"})
        self.assertIsNone(MOD._head_mismatch(2, json.dumps({"headRefOid": "a" * 40}), "a" * 40))
        message = MOD._head_mismatch(2, meta, "a" * 40)
        self.assertIn("PR #2 moved", message)
        self.assertIn("a" * 12, message)
        self.assertIn("b" * 12, message)


class ArtifactPaths(unittest.TestCase):
    """One directory per run is what replaced the bookkeeping that kept losing output."""

    def test_two_runs_of_one_pr_share_no_artifact_path(self):
        a, _ = _load(pr=7, run_id="run-A")
        b, _ = _load(pr=7, run_id="run-B")
        for path in ("ART", "DIFF", "META", "MERGED", "FINAL"):
            self.assertNotEqual(getattr(a, path), getattr(b, path), path)
        # Same PR, so the checkout is still shared -- the per-PR lock covers that.
        self.assertEqual(a.WT, b.WT)
        self.assertTrue(a.ART.endswith("/pr-7/run-A"))

    def test_a_resume_lands_on_its_own_directory(self):
        first, _ = _load(pr=7, run_id="run-A")
        resumed, _ = _load(pr=7, run_id="run-A")
        self.assertEqual(first.ART, resumed.ART)

    def test_running_the_script_by_hand_still_has_a_home(self):
        module, _ = _load(pr=7, run_id=None)
        self.assertTrue(module.ART.endswith("/pr-7/no-run-id"))


class JuryTargets(unittest.TestCase):
    def test_a_judge_never_receives_a_finding_it_had_a_hand_in(self):
        # `sources != [key]` passed a two-name finding back to BOTH its authors, stripped
        # of the id prefix, under a prompt saying "None of them are yours". Only _dedup
        # puts two names on a finding now and those skip the jury as corroborated, so
        # this is the belt to that braces -- stated as the rule it is.
        shared = dict(_finding("claude", 1), sources=["claude", "codex"],
                      corroborated=False, independent_sources=2)
        self.assertEqual(MOD._jury_targets([shared], "claude"), [])
        self.assertEqual(MOD._jury_targets([shared], "codex"), [])
        self.assertEqual(len(MOD._jury_targets([shared], "opencode")), 1)

    def test_a_solo_finding_goes_to_the_other_two(self):
        deduped = MOD._dedup([_finding("claude", 1)])
        self.assertEqual(MOD._jury_targets(deduped, "claude"), [])
        self.assertEqual(len(MOD._jury_targets(deduped, "codex")), 1)
        self.assertEqual(len(MOD._jury_targets(deduped, "opencode")), 1)

    def test_a_corroborated_finding_goes_to_nobody(self):
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1)])
        for key in ("claude", "codex", "opencode"):
            self.assertEqual(MOD._jury_targets(deduped, key), [])


class SecondAttempt(unittest.TestCase):
    """`_twice`: what makes a step worth running again, and what does not.

    It exists because a step can come back `completed` having written nothing -- CAO ends
    one on a single reading of COMPLETED, and a model thinking between tool calls looks
    exactly like a pane that has gone quiet. These pin the parts that would fail silently
    if they drifted: exactly two attempts and no more, an honest empty answer is not one
    of them, the retry's id carries the generation so a resume can repair the case this
    exists for, and the mark names the step rather than the attempt.
    """

    def _harness(self, writes_on=(), errors=None):
        """A `run`/`load` pair over a fake disk, plus the call and mark logs.

        `run` writes a finding for any step id in `writes_on` and returns the error text
        in `errors` for any id that has one, exactly as `_step_or_error` would.
        """
        disk, calls, marked = [], [], []
        errors = errors or {}

        def run(step_id):
            calls.append(step_id)
            if step_id in errors:
                return errors[step_id]
            if step_id in writes_on:
                disk.append(["a finding"])
            return None

        return run, (lambda: disk[-1] if disk else None), calls, marked

    def test_a_step_that_delivered_is_not_run_again(self):
        run, load, calls, marked = self._harness(writes_on={"r1-codex"})
        value, err = MOD._twice(run, load, "r1-codex", "7", marked.append)
        self.assertEqual((value, err), (["a finding"], None))
        self.assertEqual(calls, ["r1-codex"])
        self.assertEqual(marked, [])

    def test_a_silent_step_is_run_once_more_and_the_retry_carries_the_generation(self):
        run, load, calls, marked = self._harness(writes_on={"r1-codex-retry-7"})
        value, err = MOD._twice(run, load, "r1-codex", "7", marked.append)
        self.assertEqual((value, err), (["a finding"], None))
        self.assertEqual(calls, ["r1-codex", "r1-codex-retry-7"])
        # The mark names the step, not the attempt: what matters is that it needed one.
        self.assertEqual(marked, ["r1-codex"])

    def test_a_resume_gets_a_fresh_retry_id(self):
        """A fixed one would be replayed from the journal, so no agent would run."""
        first = self._harness()
        MOD._twice(first[0], first[1], "r1-codex", "1", first[3].append)
        later = self._harness()
        MOD._twice(later[0], later[1], "r1-codex", "2", later[3].append)
        self.assertEqual(first[2][1], "r1-codex-retry-1")
        self.assertEqual(later[2][1], "r1-codex-retry-2")

    def test_two_silent_attempts_are_all_it_gets(self):
        run, load, calls, marked = self._harness()
        value, err = MOD._twice(run, load, "r1-codex", "7", marked.append)
        self.assertEqual((value, err), (None, None))
        self.assertEqual(calls, ["r1-codex", "r1-codex-retry-7"])
        self.assertEqual(marked, ["r1-codex"])

    def test_an_empty_answer_is_an_answer(self):
        """Round 1 delivers [] for "found nothing"; round 2 hands _twice None instead."""
        for empty in ([], {}, ""):
            with self.subTest(empty=empty):
                calls, marked = [], []
                value, err = MOD._twice(lambda step_id: calls.append(step_id),
                                        lambda: empty, "r1-codex", "7", marked.append)
                self.assertEqual((value, err), (empty, None))
                self.assertEqual(calls, ["r1-codex"])
                self.assertEqual(marked, [])

    def test_a_first_attempt_that_failed_is_not_retried(self):
        """A step that raised is a different thing from a step that stayed silent."""
        run, load, calls, marked = self._harness(errors={"r1-codex": "boom"})
        value, err = MOD._twice(run, load, "r1-codex", "7", marked.append)
        self.assertEqual((value, err), (None, "boom"))
        self.assertEqual(calls, ["r1-codex"])
        self.assertEqual(marked, [])

    def test_a_retry_that_failed_surfaces_its_error(self):
        run, load, calls, marked = self._harness(errors={"r1-codex-retry-7": "boom"})
        value, err = MOD._twice(run, load, "r1-codex", "7", marked.append)
        self.assertEqual((value, err), (None, "boom"))
        self.assertEqual(calls, ["r1-codex", "r1-codex-retry-7"])
        self.assertEqual(marked, ["r1-codex"])


class StepIdentity(unittest.TestCase):
    """`_digest`: a step whose input changed has to be a different step.

    Round 2's anonymous ids are positional, so `f-01` names a different finding as soon
    as a judge's target list changes -- which a resume can now do, because a round-1
    retry really executes there. CAO replays a completed step whose call fingerprint
    matches and halts loudly on one that does not, so the id has to move with the input.
    """

    def test_the_same_payload_names_the_same_step(self):
        payload = [{"id": "f-01", "file": "a.py", "line": 10}]
        self.assertEqual(MOD._digest(payload), MOD._digest(list(payload)))

    def test_key_order_is_not_a_change(self):
        self.assertEqual(MOD._digest([{"a": 1, "b": 2}]), MOD._digest([{"b": 2, "a": 1}]))

    def test_a_judge_whose_targets_moved_gets_a_new_id(self):
        """The defect this closes: same id, new numbering, old verdicts read anyway."""
        before = MOD._dedup([_finding("claude", 1)])
        after = MOD._dedup([_finding("claude", 1), _finding("codex", 2, file="b.py")])
        first, _ = MOD._anonymize(MOD._jury_targets(before, "opencode"))
        later, _ = MOD._anonymize(MOD._jury_targets(after, "opencode"))
        self.assertNotEqual(MOD._digest(first), MOD._digest(later))

    def test_an_untouched_judge_keeps_its_id_so_its_verdicts_still_replay(self):
        first, _ = MOD._anonymize(MOD._jury_targets(MOD._dedup([_finding("claude", 1)]),
                                                    "opencode"))
        again, _ = MOD._anonymize(MOD._jury_targets(MOD._dedup([_finding("claude", 1)]),
                                                    "opencode"))
        self.assertEqual(MOD._digest(first), MOD._digest(again))

    def test_a_judges_files_and_its_step_id_move_together(self):
        """Either alone is a bug: renaming only the files under a fixed id is DIVERGED."""
        payload = [{"id": "f-01", "file": "a.py"}]
        moved = [{"id": "f-01", "file": "b.py"}]
        before = MOD._jury_artifacts("/art", "claude", payload)
        unchanged = MOD._jury_artifacts("/art", "claude", list(payload))
        after = MOD._jury_artifacts("/art", "claude", moved)
        self.assertEqual(before, unchanged)
        for was, now in zip(before, after):
            self.assertNotEqual(was, now)

    def test_a_judges_files_are_where_the_run_keeps_them(self):
        infile, mapfile, outfile, step_id = MOD._jury_artifacts("/art", "claude", [])
        for path in (infile, mapfile, outfile):
            self.assertTrue(path.startswith(os.path.join("/art", "round2") + os.sep), path)
        self.assertTrue(step_id.startswith("r2-claude-jury-"), step_id)
        self.assertTrue(mapfile.endswith("-map.json"), mapfile)

    def test_the_merge_moves_with_its_candidate_list(self):
        one = [{"id": "claude-1", "title": "a"}]
        two = one + [{"id": "codex-1", "title": "b"}]
        before = MOD._merge_artifacts("/art", one)
        after = MOD._merge_artifacts("/art", two)
        self.assertEqual(before, MOD._merge_artifacts("/art", list(one)))
        for was, now in zip(before, after):
            self.assertNotEqual(was, now)
        self.assertTrue(before[2].startswith("merge-semantic-"), before[2])

    def test_the_arbiter_moves_when_a_harness_stops_being_a_failure(self):
        """The defect #7 closed: the repaired execution's prompt no longer lists it."""
        findings = [{"id": "claude-1"}]
        failed = {"codex": "no usable findings file"}
        self.assertNotEqual(MOD._arbiter_artifacts("/art", findings, failed),
                            MOD._arbiter_artifacts("/art", findings, {}))

    def test_the_arbiter_moves_when_the_findings_move_under_the_same_failures(self):
        """They reach it through a file the prompt only names, so nothing else would."""
        self.assertNotEqual(
            MOD._arbiter_artifacts("/art", [{"id": "claude-1"}], {}),
            MOD._arbiter_artifacts("/art", [{"id": "claude-1"}, {"id": "codex-1"}], {}))

    def test_an_arbiter_given_the_same_thing_twice_replays(self):
        findings, failures = [{"id": "claude-1"}], {"codex": "silent"}
        self.assertEqual(MOD._arbiter_artifacts("/art", findings, failures),
                         MOD._arbiter_artifacts("/art", list(findings), dict(failures)))

    def test_the_arbiters_report_moves_with_its_id(self):
        """A fixed report path let a silent arbiter inherit the last execution's review."""
        findings = [{"id": "claude-1"}]
        before, before_step = MOD._arbiter_artifacts("/art", findings, {"codex": "silent"})
        after, after_step = MOD._arbiter_artifacts("/art", findings, {})
        self.assertNotEqual(before, after)
        self.assertNotEqual(before_step, after_step)
        self.assertTrue(before.startswith(os.path.join("/art", "final-review-")), before)
        self.assertTrue(before.endswith(".md"), before)


class RetryMarkers(unittest.TestCase):
    """`_mark_retried` / `_retried`: the record has to outlive the execution that made it.

    After a resume the retry's own output is already on disk, so the branch that records
    one is never entered. An in-memory list reported no retries for a run that had needed
    one, and that is the bug these two exist to keep from coming back.
    """

    def test_a_marker_is_named_after_the_step_and_outlives_its_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            MOD._mark_retried(directory, "r1-codex")
            self.assertEqual(os.listdir(directory), ["r1-codex"])
            # A second execution over the same artifacts: nothing marks, and it still reads.
            self.assertEqual(MOD._retried(directory), ["r1-codex"])

    def test_marks_come_back_in_a_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            for step_id in ("r2-opencode-jury-ab12cd34ef", "r1-codex"):
                MOD._mark_retried(directory, step_id)
            self.assertEqual(MOD._retried(directory),
                             ["r1-codex", "r2-opencode-jury-ab12cd34ef"])

    def test_a_run_that_needed_none_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(MOD._retried(directory), [])


class Wiring(unittest.TestCase):
    """The call sites the suite cannot import, read as source instead.

    `_jury_artifacts`, `_merge_artifacts` and `_arbiter_artifacts` are tested above as
    functions, but everything that USES them is below the `__main__` guard, where `_load`
    never reaches. Reverting a call site to a fixed step id therefore left the whole suite
    green -- which is how the arbiter shipped unbound after the commit whose title said it
    had been bound. These read the file, because the binding is only worth anything at the
    call.
    """

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(open(SCRIPT, encoding="utf-8").read())
        cls.calls = [n for n in ast.walk(cls.tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]

    def _calls_to(self, name):
        return [c for c in self.calls if c.func.id == name]

    def _unpacked_from(self, name, func):
        """The assignment that binds `name` out of a call to `func`, or None."""
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            if getattr(node.value.func, "id", None) != func:
                continue
            for target in node.targets:
                names = target.elts if isinstance(target, ast.Tuple) else [target]
                if any(isinstance(n, ast.Name) and n.id == name for n in names):
                    return node
        return None

    def test_no_step_is_given_a_step_id_that_cannot_move(self):
        """A literal is the regression: same id, changed input, DIVERGED or a stale replay."""
        calls = self._calls_to("step")
        self.assertTrue(calls, "no step() call found -- this test has gone blind")
        for call in calls:
            given = [kw.value for kw in call.keywords if kw.arg == "step_id"]
            self.assertEqual(len(given), 1,
                             "step() at line %d does not name a step_id" % call.lineno)
            self.assertNotIsInstance(given[0], ast.Constant,
                                     "step() at line %d takes a literal step id" % call.lineno)

    def test_the_judge_and_the_arbiter_run_under_their_bound_ids(self):
        passed = [call.args[2].id for call in self._calls_to("_twice")
                  if len(call.args) > 2 and isinstance(call.args[2], ast.Name)]
        self.assertIn("jury_step", passed)
        self.assertIn("_arbiter_step", passed)
        self.assertIsNotNone(self._unpacked_from("jury_step", "_jury_artifacts"))
        self.assertIsNotNone(self._unpacked_from("_arbiter_step", "_arbiter_artifacts"))

    def test_the_merge_runs_under_its_bound_id(self):
        self.assertIsNotNone(self._unpacked_from("_merge_step", "_merge_artifacts"))
        ids = [kw.value for call in self._calls_to("step") for kw in call.keywords
               if kw.arg == "step_id" and isinstance(kw.value, ast.Name)]
        self.assertIn("_merge_step", [n.id for n in ids])

    def test_the_arbiters_id_is_taken_before_the_call_that_uses_it(self):
        """Later, and `failures` has the arbiter's own error in it -- a different digest."""
        bound = self._unpacked_from("_arbiter_step", "_arbiter_artifacts")
        using = [call for call in self._calls_to("_twice")
                 if any(isinstance(a, ast.Name) and a.id == "_arbiter_step"
                        for a in call.args)]
        self.assertEqual(len(using), 1)
        self.assertLess(bound.lineno, using[0].lineno)


if __name__ == "__main__":
    unittest.main()
