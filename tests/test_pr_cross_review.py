"""Unit tests for the pure half of workflows/pr_cross_review.py.

    python3 -m unittest discover -s tests

Standard library only, no container and no network: everything here is a function of
plain data. That is the half of the workflow where a mistake is invisible -- a wrong
path or a wrongly merged finding does not raise, it produces a confident review of the
wrong thing after fifteen minutes and three live model sessions.
"""
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


if __name__ == "__main__":
    unittest.main()
