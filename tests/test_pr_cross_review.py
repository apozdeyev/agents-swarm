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


def _load(repo="owner/name", pr=1):
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
        self.assertEqual(MOD._read_list(path, "verdicts"),
                         [{"id": "f-01", "verdict": "confirmed"}])

    def test_null_under_the_key_is_an_empty_list(self):
        self.assertEqual(MOD._read_list(self._write('{"verdicts": null}'), "verdicts"), [])

    def test_object_under_the_key_is_an_empty_list(self):
        self.assertEqual(MOD._read_list(self._write('{"merges": {"a": 1}}'), "merges"), [])

    def test_missing_file_is_an_empty_list(self):
        self.assertEqual(MOD._read_list(os.path.join(self.dir, "nope.json"), "merges"), [])


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

    def test_one_harness_alone_is_not_corroborated(self):
        out = MOD._dedup([_finding("claude", 1)])
        self.assertFalse(out[0]["corroborated"])
        self.assertFalse(out[0]["merged_semantically"])


class ApplyMerges(unittest.TestCase):
    def test_cross_source_group_folds_without_becoming_corroborated(self):
        # The merge is one model's opinion that two write-ups are one defect. Counting
        # it as corroboration would skip the jury on the strength of that opinion.
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1, line=200)])
        out = MOD._apply_merges(deduped, [["claude-1", "codex-1"]])
        self.assertEqual([f["id"] for f in out], ["claude-1"])
        self.assertEqual(out[0]["sources"], ["claude", "codex"])
        self.assertTrue(out[0]["merged_semantically"])
        self.assertFalse(out[0]["corroborated"])
        self.assertEqual(out[0]["independent_sources"], 1)

    def test_same_source_group_is_refused(self):
        # The prompt forbids it, nothing checked, and the loser vanished from the run:
        # title, detail and failure scenario gone, only the id kept in merged_ids.
        deduped = MOD._dedup([_finding("claude", 1), _finding("claude", 2, line=200)])
        out = MOD._apply_merges(deduped, [["claude-1", "claude-2"]])
        self.assertEqual([f["id"] for f in out], ["claude-1", "claude-2"])
        self.assertFalse(out[0]["merged_semantically"])

    def test_unknown_ids_and_junk_groups_are_ignored(self):
        deduped = MOD._dedup([_finding("claude", 1)])
        out = MOD._apply_merges(deduped, [["claude-1", "codex-99"], "claude-1", []])
        self.assertEqual([f["id"] for f in out], ["claude-1"])
        self.assertFalse(out[0]["merged_semantically"])

    def test_round_one_corroboration_survives_a_merge(self):
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1),
                              _finding("opencode", 1, line=200)])
        out = MOD._apply_merges(deduped, [["claude-1", "opencode-1"]])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["corroborated"])
        self.assertEqual(out[0]["independent_sources"], 2)
        self.assertEqual(out[0]["sources"], ["claude", "codex", "opencode"])

    def test_a_repeated_id_does_not_delete_the_keeper(self):
        # A model writing ["claude-1", "claude-1", "codex-1"] put the same object in
        # `members` twice, so the keeper was also members[1] and the loop dropped its
        # own id. Both findings then left the run without a word.
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1, line=200)])
        out = MOD._apply_merges(deduped, [["claude-1", "claude-1", "codex-1"]])
        self.assertEqual([f["id"] for f in out], ["claude-1"])
        self.assertEqual(out[0]["sources"], ["claude", "codex"])

    def test_a_repeated_id_cannot_stand_in_for_a_second_member(self):
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1, line=200)])
        out = MOD._apply_merges(deduped, [["claude-1", "claude-1"]])
        self.assertEqual([f["id"] for f in out], ["claude-1", "codex-1"])
        self.assertFalse(out[0]["merged_semantically"])

    def test_same_source_group_is_refused_even_when_one_is_corroborated(self):
        # `sources` stops being an author list once _dedup has run: claude-1 carries
        # codex's name too, which used to satisfy a "two distinct sources" union and let
        # one reviewer's two findings merge -- the exact case the prompt forbids.
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1),
                              _finding("claude", 2, line=200)])
        self.assertEqual(deduped[0]["sources"], ["claude", "codex"])
        out = MOD._apply_merges(deduped, [["claude-1", "claude-2"]])
        self.assertEqual([f["id"] for f in out], ["claude-1", "claude-2"])

    def test_merged_findings_are_json_serialisable(self):
        # merged.json is what the arbiter reads; a field the encoder chokes on would
        # take the run down after round 2.
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1, line=200)])
        out = MOD._apply_merges(deduped, [["claude-1", "codex-1"]])
        self.assertIn('"merged_semantically": true', json.dumps(out))


class JuryTargets(unittest.TestCase):
    def _merged(self):
        deduped = MOD._dedup([_finding("claude", 1), _finding("codex", 1, line=200)])
        return MOD._apply_merges(deduped, [["claude-1", "codex-1"]])

    def test_a_merged_finding_never_goes_back_to_its_authors(self):
        # The regression: `sources != [key]` is true for BOTH authors of a two-name
        # finding, so each was handed its own work to rule on, anonymised, under a
        # prompt that says "None of them are yours".
        merged = self._merged()
        self.assertEqual(MOD._jury_targets(merged, "claude"), [])
        self.assertEqual(MOD._jury_targets(merged, "codex"), [])
        self.assertEqual([f["id"] for f in MOD._jury_targets(merged, "opencode")],
                         ["claude-1"])

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
