"""pr_cross_review - three harnesses review a PR, then judge each other's findings.

Round 1 fans out Claude, Codex and OpenCode/DeepSeek over the same diff. Findings are
normalized and deduped, and provenance is kept: a finding two or more harnesses
reported independently is already cross-confirmed and skips round 2. The contested
remainder goes to a full jury -- every harness judges the findings of both others, so
each contested finding collects two independent verdicts and a disagreement between
them is itself a signal. A Claude arbiter writes the final report.

Takes a repository and a PR number and provisions the checkout itself: a bare clone per
repository, a detached worktree per PR at refs/pull/<n>/head. Pointing the harnesses at
an existing working copy is what this replaces -- that copy sits on whatever branch it
was left on, so every file opened for context was the wrong version of itself.

Determinism note: resume re-executes this file top-to-bottom, so every path derives
from the inputs and nothing here reads the clock or an RNG.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from cao_workflow import ShimError, ShimHTTPError, emit_output, get_inputs, step

INPUTS = {
    # "owner/name", or any GitHub URL containing it. The wrapper turns a PR URL into
    # this plus `pr`, so nothing clever is needed here.
    "repo": {"type": "string", "required": True},
    "pr": {"type": "int", "required": True},
    "max_workers": {"type": "int", "required": False, "default": 3},
    "step_timeout": {"type": "int", "required": False, "default": 900},
}

_inputs = get_inputs()
PR = int(_inputs["pr"])
MAX_WORKERS = int(_inputs.get("max_workers", 3))
TIMEOUT = float(_inputs.get("step_timeout", 900))

_repo_raw = str(_inputs["repo"]).strip().rstrip("/")
if _repo_raw.endswith(".git"):
    _repo_raw = _repo_raw[:-4]
# ":" for scp-style remotes, so git@github.com:owner/name lands the same as a URL.
_repo_parts = [p for p in _repo_raw.replace(":", "/").split("/") if p]
# A pasted PR URL ends in .../pull/<n>[/files]. The wrapper strips that, but this script
# is documented as directly runnable, and there the mistake costs a confusing
# "Repository not found: pull/1" instead of a review.
if "pull" in _repo_parts:
    _repo_parts = _repo_parts[:_repo_parts.index("pull")]
if len(_repo_parts) < 2:
    raise SystemExit("repo must be owner/name or a github.com URL, got %r" % _inputs["repo"])
OWNER, NAME = _repo_parts[-2], _repo_parts[-1]
SLUG = "%s__%s" % (OWNER, NAME)

WORKSPACE = "/home/cao/workspace"
# One bare clone per repository, shared by every PR of it, and one detached worktree per
# PR. A worktree rather than a checkout in a shared clone: two reviews of the same repo
# must not fight over HEAD, and the repo must not be left on some review's branch.
BARE = os.path.join(WORKSPACE, ".cao-repos", "%s.git" % SLUG)
WT = os.path.join(WORKSPACE, ".cao-worktrees", SLUG, "pr-%d" % PR)
# Every agent step runs here. Named REPO because that is what the prompts call it.
REPO = WT
# Outside the checkout, so a review run never dirties the git tree. Keyed by owner as
# well as name: two repos of the same name from different owners are different repos.
ART = os.path.join(WORKSPACE, ".cao-review", SLUG, "pr-%d" % PR)
DIFF = os.path.join(ART, "diff.patch")
META = os.path.join(ART, "meta.json")
MERGED = os.path.join(ART, "merged.json")
FINAL = os.path.join(ART, "final-review.md")

# The stock `reviewer` role is read-only, and a read-only agent told to write a file
# hangs the entire step budget. Grant fs_write per step instead of forking profiles.
WRITE_TOOLS = ["@builtin", "fs_read", "fs_list", "fs_write", "@cao-mcp-server"]

SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("correctness", "security", "performance", "maintainability", "test-coverage")

# (key, provider, agent) -- sorted, so the item->step_id mapping is stable on resume.
HARNESSES = (
    ("claude", "claude_code", "reviewer"),
    ("codex", "codex", "reviewer_codex"),
    ("opencode", "opencode_cli", "reviewer_opencode"),
)


def _read_json(path, default):
    """Parse a file an agent wrote. Tolerates code fences and surrounding prose."""
    if not os.path.exists(path):
        return default
    raw = open(path, encoding="utf-8", errors="replace").read().strip()
    raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except ValueError:
        brace = re.search(r"[\[{].*[\]}]", raw, re.S)
        if not brace:
            return default
        try:
            return json.loads(brace.group(0))
        except ValueError:
            return default


def _read_items(path, key):
    """The list under `key` in an agent-written JSON file, or None when there is none.

    A model asked for {"<key>": [...]} sometimes writes the bare array instead, or the
    key with a null under it. Both used to reach `.get`/`for` unguarded -- an array is
    truthy, so `or {}` never fired -- and the AttributeError escaped the pool and killed
    a run that had already paid for round 1.

    None rather than []: an empty list is an answer a step is invited to give, and
    "wrote nothing" is a failure. Round 1 has always drawn that line; flattening both to
    [] is what let the merge step return `completed` having written nothing and have it
    pass for "no duplicates found".
    """
    data = _read_json(path, None)
    items = data.get(key) if isinstance(data, dict) else data
    return items if isinstance(items, list) else None


def _norm(finding, source, index):
    """Coerce one agent-authored finding into the shape the rest of the script assumes."""
    path = str(finding.get("file") or "").strip()
    if os.path.isabs(path):
        path = os.path.relpath(path, REPO)
    # A prefix, not a character set: str.lstrip("./") turned ".dockerignore" into
    # "dockerignore" and ".github/workflows/ci.yml" into "github/workflows/ci.yml".
    # That corrupted path is what the round-2 judge is handed and what the arbiter
    # cites, so a real defect in a dotfile came back needs-context every time.
    if path.startswith("./"):
        path = path[2:]
    try:
        line = int(finding.get("line"))
    except (TypeError, ValueError):
        line = None
    severity = str(finding.get("severity") or "").lower()
    category = str(finding.get("category") or "").lower()
    return {
        "id": "%s-%d" % (source, index),
        "sources": [source],
        "file": path,
        "line": line,
        "severity": severity if severity in SEVERITIES else "medium",
        "category": category if category in CATEGORIES else "correctness",
        "title": str(finding.get("title") or "").strip()[:200],
        "detail": str(finding.get("detail") or "").strip(),
        "failure_scenario": str(finding.get("failure_scenario") or "").strip(),
        "confidence": str(finding.get("confidence") or "medium").lower(),
    }


def _load_round1(path, source):
    """Findings from one harness, or None when it delivered no usable file.

    An empty list and None are different answers. "I reviewed this and found nothing"
    is a useful result the prompt explicitly invites; "the step ended without writing
    anything" is a failure that used to be recorded as the former.
    """
    items = _read_items(path, "findings")
    if items is None:
        return None
    return [_norm(f, source, i + 1) for i, f in enumerate(items) if isinstance(f, dict)]


def _dedup(findings):
    """Collapse the findings two harnesses filed at the same place into one.

    Same file, same category, same line. Proximity used to count as well -- lines
    within three of each other -- which merged a missing bounds check at :100 with a
    wrong return value at :102, discarded the loser's text outright and then marked the
    survivor corroborated: two reviewers who found two different bugs produced one
    auto-confirmed claim. Near misses go to the semantic pass instead, which reads the
    descriptions rather than guessing from a line number.
    """
    deduped = []
    for finding in findings:
        for kept in deduped:
            same_place = (
                kept["file"] == finding["file"]
                and kept["category"] == finding["category"]
                and kept["line"] is not None
                and kept["line"] == finding["line"]
            )
            if same_place:
                for src in finding["sources"]:
                    if src not in kept["sources"]:
                        kept["sources"].append(src)
                kept["merged_ids"] = kept.get("merged_ids", []) + [finding["id"]]
                break
        else:
            deduped.append(finding)
    for finding in deduped:
        # Frozen here, before the semantic pass can union more names into `sources`:
        # this is the flag that skips round 2, and only agreement the harnesses reached
        # on their own -- same defect, same line, no model in between -- may do that.
        finding["independent_sources"] = len(finding["sources"])
        finding["corroborated"] = finding["independent_sources"] > 1
        finding["merged_semantically"] = False
    return deduped


def _apply_merges(deduped, groups):
    """Fold each group of ids into its first member, and return what survives.

    A group has to span two distinct sources. The merge prompt already forbids grouping
    two findings from one reviewer, but nothing checked, and a group that slipped
    through dropped its other members outright -- title, detail and failure scenario
    gone from the run, only the id left behind in `merged_ids`.
    """
    by_id = {f["id"]: f for f in deduped}
    dropped = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        # Unique ids, order kept. A repeated id -- an ordinary model slip, and one the
        # prompt says nothing against -- put the same object in `members` twice, so the
        # keeper was also members[1] and the loop dropped its own id: both findings then
        # vanished from the run, silently, with `len(members) >= 2` satisfied by the
        # repetition alone.
        members = [by_id[i] for i in dict.fromkeys(group) if i in by_id and i not in dropped]
        # Every member must bring authors none of the others has. A union of two was not
        # that test: `sources` stops being an author list once `_dedup` has run -- a
        # finding two harnesses filed at the same line carries both names -- so a group
        # of two Claude findings passed whenever either of them was corroborated, which
        # is exactly the same-reviewer merge the prompt forbids.
        authors = [s for m in members for s in m["sources"]]
        if len(members) < 2 or len(set(authors)) != len(authors):
            continue
        keeper = members[0]
        for extra in members[1:]:
            for src in extra["sources"]:
                if src not in keeper["sources"]:
                    keeper["sources"].append(src)
            keeper["merged_ids"] = keeper.get("merged_ids", []) + [extra["id"]]
            # The other write-ups, not just their ids. A merge that spans every harness
            # leaves nobody eligible to judge the survivor -- `_jury_targets` excludes
            # each of them as an author -- so the arbiter is the only reader who can
            # still ask whether these really are one defect, and it needs the words to
            # do it. Losing them also lost whatever the folded finding said better.
            keeper["merged_from"] = keeper.get("merged_from", []) + [
                {k: extra[k] for k in ("id", "sources", "file", "line",
                                       "title", "detail", "failure_scenario")}]
            dropped.add(extra["id"])
        # Deliberately not `corroborated`: one model decided these describe the same
        # defect. That is a claim for the arbiter to weigh, not the independent
        # agreement that earns a finding a pass on the jury.
        keeper["merged_semantically"] = True
    return [f for f in deduped if f["id"] not in dropped]


def _jury_targets(deduped, key):
    """The findings `key` is allowed to judge: contested, and none of them its own.

    `sources != [key]` was not that test. A finding the semantic merge grouped carries
    two names, so it matched neither author's one-name list and went back to BOTH of
    them to rule on -- with `JUDGE_FIELDS` stripping the id prefix and `sources`, and
    the prompt telling each judge "None of them are yours". Only reachable since
    corroboration was frozen in `_dedup`: a merged finding used to skip round 2 outright.
    """
    return [f for f in deduped if not f["corroborated"] and key not in f["sources"]]


def _worktrees_to_drop(entries, own, has_report, take_lock):
    """Which sibling checkouts may be removed, each with the lock that keeps it safe.

    A report on disk is not proof the review is over: a re-review of that PR finds its
    diff and worktree already there and skips setup, so last time's final-review.md sits
    beside three agents at work. The lock is the live signal. It stays HELD in what comes
    back -- releasing it here would reopen the window this closes, between the decision
    and the `git worktree remove` the caller makes on it.
    """
    keep = []
    for entry in sorted(entries):
        if entry == own or not entry.startswith("pr-"):
            continue
        if not has_report(entry):
            continue
        held = take_lock(entry)
        if held is not None:
            keep.append((entry, held))
    return keep


def _head_mismatch(pr, meta, checked_out):
    """The refusal for a run whose diff and checkout disagree, or None when they agree.

    `gh pr diff` reads the live PR, so it can describe a commit newer than the one that
    was fetched -- reviewing a diff against files it does not match is the failure this
    stage exists to prevent, and caching the mismatch would hand it to every resume too.
    """
    meta_head = json.loads(meta).get("headRefOid", "")
    if meta_head == checked_out:
        return None
    return ("PR #%d moved while it was being set up: the worktree is at %s, the diff "
            "describes %s. Re-run -- the new head is picked up then."
            % (pr, checked_out[:12], meta_head[:12]))


def _exit_note(code, final, failures):
    """What a failing run writes to stderr on its way out, or "" when it succeeded.

    A non-zero exit makes CAO drop the sentinel `emit_output` just wrote -- the run is
    recorded FAILED and its output goes with it -- and the stderr tail is what the run
    record keeps in its place. So it carries the two things a reader is left needing:
    which stage did not deliver, and where the report that WAS produced is. The path
    only when the file exists, because the arbiter failing to write it is one of the
    ways to get here and naming a path that is not there is worse than saying nothing.
    """
    if not code:
        return ""
    note = []
    if failures:
        note.append("failures: %s\n" % json.dumps(failures, sort_keys=True))
    if os.path.exists(final):
        note.append("final review: %s\n" % final)
    return "".join(note)


def _take_lock(path):
    """Take an exclusive lock on `path`, or return None when someone else holds it.

    The handle IS the lock: it has to outlive the caller's frame, because closing it --
    or letting it be collected -- releases it. That is also the point. A lock dies with
    the process holding it, so a run that crashed leaves nothing to be cleaned up by
    hand, and a stale file never reads as a live run.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _is_resume_of(run_id, pinned, on_disk):
    """Whether this execution is a resume of the run that left the snapshot on disk.

    All three have to hold. A resume re-executes this file in a new process under the
    SAME run id, so the id is the seam between the two things stage 0 must do at once:
    a new run has to notice the PR gained commits and re-read it, a resume has to keep
    the snapshot it started from. An absent run id -- the script run by hand -- is never
    a resume: guessing wrong in that direction deletes a run's own output.
    """
    return bool(run_id) and on_disk and pinned.get("run_id") == run_id


def _artifacts_are_stale(run_id, is_resume, provisioned):
    """Whether round1/ and round2/ hold a PREVIOUS run's results rather than this one's.

    `_review` and `_validate` read their results off the disk because a step can come
    back `completed` having written nothing -- and a file the last review of this PR
    left behind satisfies that check just as well, so the failure this pipeline exists
    to expose is recorded as a successful review of stale findings. True when the
    checkout was re-read, and equally when a distinct run starts against a head that has
    not moved. Never on a resume: its steps are already recorded completed and will not
    write again, so clearing there throws away the run's own paid-for output.
    """
    return bool(provisioned or (run_id and not is_resume))


def _clear_artifacts(art, files):
    """Delete a previous run's outputs: the named files, and both round directories."""
    for path in files:
        if os.path.exists(path):
            os.remove(path)
    for sub in ("round1", "round2"):
        directory = os.path.join(art, sub)
        if os.path.isdir(directory):
            for leftover in sorted(os.listdir(directory)):
                os.remove(os.path.join(directory, leftover))


def _empty_report(pr, delivered, failures):
    """The report for a run that found nothing.

    Names the harnesses that actually delivered and lists the ones that did not. The
    fixed sentence it replaced claimed all three had reviewed the diff whatever
    happened, and `failures` reached the JSON output alone -- so on the one path that
    writes a report without an arbiter, two silent harnesses still produced a clean bill
    of health from three.
    """
    text = ["# PR #%d cross-review\n" % pr,
            "\nNo findings: %s reviewed the diff and reported no defect.\n"
            % ", ".join(delivered)]
    if failures:
        text.append("\n**Did not deliver:**\n\n")
        text.extend("- `%s` - %s\n" % (name, err) for name, err in sorted(failures.items()))
    return "".join(text)


def _report_is_usable(path):
    """Whether the arbiter left a report behind at all.

    Both rounds verify their artifact on disk; this is that check for the one deliverable
    the run exists to produce, which had none -- a step that replied in the terminal
    without writing the file was reported as a success pointing at a path that does not
    exist.

    Structural, not a size floor. The 200-byte floor it replaces failed a report that
    was short because there was little to say: when the jury rejects every finding, the
    honest review is a heading and one sentence, and a correct run was then recorded
    FAILED with its structured output dropped. Non-delivery looks like no file, an empty
    one, or a lone heading -- two lines with something on them separate that from brevity.
    """
    if not os.path.exists(path):
        return False
    text = open(path, encoding="utf-8", errors="replace").read()
    return len([line for line in text.splitlines() if line.strip()]) >= 2


# Everything above this line is pure: no network, no filesystem, no clock. Everything
# below clones a repository and drives three model harnesses. CAO runs this file as a
# script (`python pr_cross_review.py`, its own process), so the guard fires only for
# tests/test_pr_cross_review.py, which loads the module for the helpers above and
# catches the exit.
if __name__ != "__main__":
    raise SystemExit(0)

# --- stage 0: check out the PR and fetch its diff ---------------------------------
# Plain Python, not an agent step. The whole stage is skipped once the diff, the
# metadata and the worktree are all present. That is what makes resume safe, and it is
# also what stops a resume from re-pointing the checkout at commits newer than the
# cached diff describes -- reviewing code that does not match the diff is the failure
# this stage exists to prevent.
PR_REF = "refs/cao/pr-%d" % PR
SLUG_PATH = "%s/%s" % (OWNER, NAME)
GIT_ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0")
LOCKS = os.path.join(WORKSPACE, ".cao-repos")

os.makedirs(os.path.join(ART, "round1"), exist_ok=True)
os.makedirs(os.path.join(ART, "round2"), exist_ok=True)


def _git(*args, **kwargs):
    """Run git and hand back the result. The caller decides whether failure matters."""
    return subprocess.run(["git"] + list(args), env=GIT_ENV,
                          capture_output=True, text=True, **kwargs)


def _git_ok(*args, **kwargs):
    done = _git(*args, **kwargs)
    if done.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), done.stderr.strip()))
    return done.stdout


def _pr_lock(directory):
    """The lock guarding one PR's checkout, taken. One place builds that path."""
    return _take_lock(os.path.join(LOCKS, "%s.%s.lock" % (SLUG, directory)))


# One lock per PR, taken before anything is touched and held for the whole run -- the
# per-repository lock below covers only the git mutations. Every path this run writes
# is keyed by PR alone (the worktree, diff.patch, round1/, round2/, merged.json), so a
# second review of the same PR would overwrite the first's material while it is still
# being read, and could remove and recreate its checkout underneath live agents. It is
# also what tells `_prune_finished_worktrees` which sibling checkouts are live: the
# lock dies with the process holding it, so a run that crashed leaves nothing to clean
# up by hand. Module-level, so the descriptor outlives this line -- closing it, or
# letting it be collected, releases the lock.
RUN_LOCK = _pr_lock("pr-%d" % PR)
if RUN_LOCK is None:
    raise SystemExit("a review of %s#%d is already running" % (SLUG_PATH, PR))


def _has_report(directory):
    """Whether some review of that PR has already written its report."""
    return os.path.exists(
        os.path.join(WORKSPACE, ".cao-review", SLUG, directory, "final-review.md"))


def _prune_finished_worktrees():
    """Drop checkouts of this repo's other PRs whose review is finished and not running.

    Disk hygiene only: artifacts, final-review.md included, are never touched -- just
    the working copy, which is reproducible from the bare clone. Completion is read off
    the filesystem rather than from a timestamp, because a workflow script that consults
    the clock diverges on resume. Which entries qualify is `_worktrees_to_drop`'s
    decision, and it hands back each one's lock still held: the removal happens inside
    that lock, not after it.
    """
    _git("worktree", "prune", cwd=BARE)
    root = os.path.join(WORKSPACE, ".cao-worktrees", SLUG)
    if not os.path.isdir(root):
        return
    for entry, held in _worktrees_to_drop(os.listdir(root), "pr-%d" % PR,
                                          _has_report, _pr_lock):
        _git("worktree", "remove", "--force", os.path.join(root, entry), cwd=BARE)
        held.close()


# Which commit this run reviews, and which run pinned it. The run id is the seam
# between the two things stage 0 has to do at once: a NEW run must notice that the PR
# gained commits since last time and re-read it, while a RESUME must keep the snapshot
# it started from -- re-reading there would put new code under findings already made
# against the old, which is the failure this whole stage exists to prevent. The run id
# is stable across a resume and different for a new run, so it tells the two apart.
SNAPSHOT = os.path.join(ART, "snapshot.json")
RUN_ID = os.environ.get("CAO_WORKFLOW_RUN_ID", "")


def _head_oid():
    """The PR's current head, from the API rather than from whatever is on disk."""
    return subprocess.run(
        ["gh", "pr", "view", str(PR), "--repo", SLUG_PATH, "--json", "headRefOid",
         "-q", ".headRefOid"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


try:
    _pinned = json.load(open(SNAPSHOT))
except (OSError, ValueError):
    _pinned = {}

_on_disk = (os.path.exists(DIFF) and os.path.exists(META)
            and os.path.exists(os.path.join(WT, ".git")))

_is_resume = _is_resume_of(RUN_ID, _pinned, _on_disk)

if _is_resume:
    # A resume of the run that took this snapshot. Keep it exactly as it was, and do
    # not ask the API anything -- the answer could have changed since.
    HEAD_OID = _pinned.get("head", "")
else:
    HEAD_OID = _head_oid()

_provisioned = not (_on_disk and _pinned.get("head") == HEAD_OID)
if _provisioned:
    os.makedirs(os.path.dirname(BARE), exist_ok=True)
    os.makedirs(os.path.dirname(WT), exist_ok=True)
    # One lock per repository, held only across the git mutations. Two reviews of
    # different PRs of the same repo otherwise race on one object store and one set of
    # worktree admin files, which fails intermittently rather than cleanly.
    with open(os.path.join(WORKSPACE, ".cao-repos", "%s.lock" % SLUG), "w") as _lock:
        fcntl.flock(_lock, fcntl.LOCK_EX)
        if not os.path.isdir(BARE):
            _git_ok("clone", "--bare", "https://github.com/%s.git" % SLUG_PATH, BARE)
        # refs/pull/<n>/head resolves for merged and closed PRs and for PRs from forks.
        # A branch name does none of those, which is how the previous version ended up
        # reviewing whatever the local clone happened to be sitting on.
        _git_ok("fetch", "--force", "origin", "pull/%d/head:%s" % (PR, PR_REF), cwd=BARE)
        _prune_finished_worktrees()
        if os.path.exists(WT):
            _git("worktree", "remove", "--force", WT, cwd=BARE)
        if os.path.exists(WT):
            # git declined because the path is not a registered worktree -- a leftover
            # from an interrupted run. The directory is ours alone, so take it out.
            shutil.rmtree(WT)
        _git_ok("worktree", "add", "--detach", WT, PR_REF, cwd=BARE)
        # What is actually in the checkout, which is not necessarily what the API named
        # a moment ago: a push landing between the two puts newer commits here.
        _checked_out = _git_ok("rev-parse", PR_REF, cwd=BARE).strip()

    _diff = subprocess.run(
        ["gh", "pr", "diff", str(PR), "--repo", SLUG_PATH],
        capture_output=True, text=True, check=True,
    ).stdout
    # The metadata is read LAST, and that ordering is the check. Asking for the head
    # before the diff only proved the PR had not moved by then and left the
    # fetch-to-diff window wide open; asking after covers everything up to the diff, and
    # a push landing later than that shows up as a mismatch and refuses the run, which
    # is the safe direction to be wrong in.
    _meta = subprocess.run(
        ["gh", "pr", "view", str(PR), "--repo", SLUG_PATH, "--json",
         "title,body,url,headRefName,headRefOid,baseRefName,files"],
        capture_output=True, text=True, check=True,
    ).stdout
    _moved = _head_mismatch(PR, _meta, _checked_out)
    if _moved:
        raise SystemExit(_moved)
    HEAD_OID = _checked_out
    open(META, "w").write(_meta)
    open(DIFF, "w").write(_diff)

if _artifacts_are_stale(RUN_ID, _is_resume, _provisioned):
    _clear_artifacts(ART, (MERGED, FINAL, os.path.join(ART, "dedup-candidates.json"),
                           os.path.join(ART, "dedup-merges.json")))

# Written even when nothing was re-read, so that a resume of THIS run recognises its own
# snapshot and leaves it alone.
open(SNAPSHOT, "w").write(json.dumps({"run_id": RUN_ID, "head": HEAD_OID}, indent=2))

# Outside the setup guard: ~/.claude.json is not on a volume, so a container recreate
# drops the trust record while the worktree on the volume survives. Untrusted, Claude
# opens a blocking dialog and the step dies on an init timeout. Cheap and idempotent.
subprocess.run(["cao-trust", WT], check=True)

FINDING_SHAPE = (
    '{"findings": [{"file": "<path relative to the repo root>", "line": <int>, '
    '"severity": "critical|high|medium|low", '
    '"category": "correctness|security|performance|maintainability|test-coverage", '
    '"title": "<one line, under 120 chars>", '
    '"detail": "<what is wrong and why it matters>", '
    '"failure_scenario": "<concrete inputs or state that produce the wrong result>", '
    '"confidence": "high|medium|low"}]}'
)

# One scale for both rounds. Without it every harness applies its own threshold and
# the `corrected_severity` values are not comparable -- a `split` then records a
# difference in calibration rather than a disagreement about the code. Phrased by
# consequence, not by project: what makes a defect critical is that its effect cannot
# be taken back, whatever the codebase does.
SEVERITY_RUBRIC = (
    "Rate severity on this scale, not your own:\n"
    "  critical - can cause a wrong or duplicated irreversible side effect (an order\n"
    "             placed, money moved, data destroyed), or silently corrupt state that\n"
    "             is later acted on\n"
    "  high     - can crash, hang or stall the process while it holds live state, or\n"
    "             regresses the latency of a path this project treats as hot\n"
    "  medium   - wrong behaviour off those paths, or data lost silently and only\n"
    "             noticed later\n"
    "  low      - everything else\n"
)

# Codex at high effort reached for `cargo test` unprompted and lost the round to
# `cargo: command not found`, the same way it lost one to a missing `rg` before. The
# container carries no compiler at all -- verified: cargo, rustc, gcc, cc, make, go and
# javac are all absent, and so is any project test runner. Saying so up front costs a
# sentence; discovering it costs a tool call and, on the evidence, sometimes the step.
ENVIRONMENT_NOTE = (
    "This container has no compiler or build toolchain: cargo, rustc, gcc, make and go "
    "are not installed, and neither is the project's test runner. python3, node, git, gh "
    "and rg are. So review statically -- read the code rather than trying to build or "
    "run it.\n\n"
)

R1_PROMPT = (
    "You are reviewing pull request #%d of the repository at %s.\n\n"
    "The unified diff is at %s and the PR metadata (title, body, changed files) is at %s. "
    "Read both. You may read any file in the repository for context.\n\n"
    + ENVIRONMENT_NOTE +
    "Report only defects you can point at in the diff: correctness bugs, security issues, "
    "resource and performance problems, missing test coverage for changed behaviour, and "
    "maintainability problems severe enough to act on. Do not report style preferences, and "
    "do not restate what the code does. Every finding needs a concrete failure scenario -- "
    "if you cannot say what input makes it go wrong, leave it out.\n\n"
    + SEVERITY_RUBRIC +
    "\nWrite your findings as a single JSON object to %s, with exactly this shape:\n%s\n\n"
    "Write raw JSON only -- no code fences, no commentary in the file. An empty findings "
    "list is a valid and useful answer. When the file is written, reply with just its path."
)


def _review(spec):
    key, provider, agent = spec
    out = os.path.join(ART, "round1", "%s.json" % key)
    prompt = R1_PROMPT % (PR, REPO, DIFF, META, out, FINDING_SHAPE)
    try:
        step(provider, agent, prompt,
             recovery="idempotent", step_id="r1-%s" % key, timeout=TIMEOUT,
             working_directory=REPO, allowed_tools=WRITE_TOOLS)
    except ShimHTTPError as exc:
        # 409 is a halt or a replay divergence -- a human decides those, never this script.
        if getattr(exc, "status", None) == 409:
            raise
        return key, [], str(exc)
    except ShimError as exc:
        return key, [], str(exc)
    # A step can report `completed` without the work having happened -- the harnesses are
    # driven through their TUI, and a judge has been seen cut short mid-reasoning, having
    # written nothing, yet still coming back completed. So the difference between
    # "reviewed, found nothing" and "never delivered" is read off the disk, not the state.
    found = _load_round1(out, key)
    if found is None:
        return key, [], "no usable findings file at %s" % out
    return key, found, None


with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    _r1 = list(pool.map(_review, sorted(HARNESSES)))

round1 = {key: found for key, found, _ in _r1}
failures = {key: err for key, _, err in _r1 if err}

if len(failures) == len(HARNESSES):
    # Exit non-zero so the run is recorded FAILED. Exiting 0 here would file a run in
    # which nothing was reviewed as `completed`, and `./cao review` would return success.
    emit_output({"pr": PR, "error": "every harness failed in round 1", "failures": failures})
    raise SystemExit(1)


def _finish(code):
    """Exit, saying on stderr what a failed run's dropped output would have said."""
    sys.stderr.write(_exit_note(code, FINAL, failures))
    raise SystemExit(code)


# --- stage 1: normalize and dedup ------------------------------------------------
# Python first: same file, same category, same line is the same finding.
deduped = _dedup([f for key in sorted(round1) for f in round1[key]])

# Then an agent pass for the duplicates Python cannot see: the same bug described in
# different words, at a nearby line, or filed under a different category.
if len([f for f in deduped if not f["corroborated"]]) > 1:
    candidates = os.path.join(ART, "dedup-candidates.json")
    merges_out = os.path.join(ART, "dedup-merges.json")
    open(candidates, "w").write(json.dumps(
        [{k: f[k] for k in ("id", "sources", "file", "line", "category", "title", "detail")}
         for f in deduped], indent=2))
    merge_prompt = (
        "The JSON array at %s holds code-review findings from three independent reviewers. "
        "Each carries a `sources` field naming which reviewer produced it.\n\n"
        "Find groups that describe THE SAME underlying defect in different words. Only group "
        "findings from DIFFERENT sources -- never merge two findings from the same reviewer. "
        "Be conservative: if two findings could be separate bugs, leave them apart.\n\n"
        'Write to %s exactly: {"merges": [["id1", "id2"], ...]}\n'
        "Raw JSON only, no code fences. An empty merges list is a valid answer. "
        "Reply with just the path when done."
    ) % (candidates, merges_out)
    try:
        step("claude_code", "reviewer", merge_prompt,
             recovery="idempotent", step_id="merge-semantic", timeout=TIMEOUT,
             working_directory=REPO, allowed_tools=WRITE_TOOLS)
        # The one stage that was not verified on disk. Every other agent step checks
        # its artifact because a step can return `completed` having written nothing --
        # and here that came back indistinguishable from an honest {"merges": []}, so
        # the dedup silently did nothing, near-duplicates went separately through round
        # 2 and into the report, and the arbiter was never told.
        _groups = _read_items(merges_out, "merges")
        if _groups is None:
            failures["merge-semantic"] = "no usable merge file at %s" % merges_out
        else:
            deduped = _apply_merges(deduped, _groups)
    except ShimHTTPError as exc:
        if getattr(exc, "status", None) == 409:
            raise
        failures["merge-semantic"] = str(exc)
    except ShimError as exc:
        failures["merge-semantic"] = str(exc)

# `corroborated` was set in `_dedup`, off round 1 alone: found independently by two or
# more harnesses IS the cross-confirmation round 2 exists to produce, so such a finding
# is confirmed there and skips validation. Two of three agreeing is stronger evidence
# than two of two was -- the finding had a real chance to go uncorroborated and did not.
# The semantic merge above unions `sources` too, and that is a different thing: one
# model's opinion that two write-ups are the same defect. It sets `merged_semantically`
# and never sets this flag. It can still cost a finding its jury, though, and quietly:
# a group spanning all three harnesses leaves every one of them an author, so
# `_jury_targets` finds nobody eligible and the finding reaches the arbiter `unjudged`.
# That is a real state, not an error -- what it must not be is silent, so it is counted
# in the output and explained in the arbiter prompt, with the folded write-ups attached
# for the one reader still able to weigh them.
for f in deduped:
    f["sources"] = sorted(f["sources"])
open(MERGED, "w").write(json.dumps(deduped, indent=2))

if not deduped:
    # The one path that writes a report with no arbiter behind it; `_empty_report` says
    # who actually delivered, and the exit code says whether anyone did not.
    _delivered = sorted(key for key, _, err in _r1 if not err)
    open(FINAL, "w").write(_empty_report(PR, _delivered, failures))
    emit_output({"pr": PR, "final_review": FINAL, "findings": 0, "failures": failures})
    _finish(1 if failures else 0)

# --- stage 2: cross-validation ---------------------------------------------------
R2_PROMPT = (
    "You are adjudicating code-review findings produced by OTHER reviewers on pull "
    "request #%d of the repository at %s. None of them are yours.\n\n"
    "The findings to judge are in the JSON array at %s. The diff under review is at %s. "
    "Read the actual code before ruling on anything.\n\n"
    + ENVIRONMENT_NOTE +
    "For each finding decide:\n"
    "  confirmed     - the defect is real; the failure scenario holds\n"
    "  rejected      - it is wrong, already handled elsewhere, or not a defect\n"
    "  needs-context - it may be real but cannot be settled from this diff alone\n\n"
    "Judge the claim, not its wording, and do not be deferential: a finding you cannot "
    "reproduce from the code in front of you is not confirmed. To reject one, name the "
    "specific code that prevents the described scenario; being unable to reproduce it "
    "is needs-context, not a rejection. Where the severity is miscalibrated, give a "
    "corrected one.\n\n"
    + SEVERITY_RUBRIC +
    "\n"
    'Write to %s exactly: {"verdicts": [{"id": "<finding id>", '
    '"verdict": "confirmed|rejected|needs-context", "reasoning": "<why, citing the code>", '
    '"corrected_severity": "critical|high|medium|low"}]}\n'
    "Raw JSON only, no code fences. Include every finding. Reply with just the path."
)


# Everything else a finding carries names its author: the `claude-`/`codex-`/`opencode-`
# prefix in `id`, the `sources` list, and `confidence` -- the reporter's own assessment
# of itself. A judge who knows who wrote a claim, and how sure they were, is no longer
# judging it independently, which is the one thing round 2 exists to do.
JUDGE_FIELDS = ("file", "line", "severity", "category", "title", "detail", "failure_scenario")


def _anonymize(targets):
    """Return (payload for the judge, anonymous id -> real id).

    Ids are positional over an already-deterministic list, so they are stable on resume.
    Each judge gets its own numbering because each sees a different subset.
    """
    payload, id_map = [], {}
    for position, finding in enumerate(targets, 1):
        anon = "f-%02d" % position
        id_map[anon] = finding["id"]
        item = {"id": anon}
        item.update({k: finding[k] for k in JUDGE_FIELDS})
        payload.append(item)
    return payload, id_map


def _validate(spec):
    """`key` is the judge; it validates every finding the other harnesses produced.

    Full jury rather than a round-robin: a contested finding is judged by both of the
    harnesses that did not report it, so it ends up with two independent verdicts and
    the judges are free to disagree. One step per judge, not one per (judge, subject)
    pair -- the judge reads all foreign findings at once, which keeps round 2 at
    len(HARNESSES) steps instead of len(HARNESSES) * (len(HARNESSES) - 1).
    """
    key, provider, agent = spec
    targets = _jury_targets(deduped, key)
    if not targets:
        return key, {}, None
    payload, id_map = _anonymize(targets)
    infile = os.path.join(ART, "round2", "to-judge-by-%s.json" % key)
    # Kept next to it so a run stays debuggable: the verdict file the judge writes is
    # in anonymous ids, and this is the only record of what they stood for.
    mapfile = os.path.join(ART, "round2", "to-judge-by-%s-map.json" % key)
    outfile = os.path.join(ART, "round2", "%s-verdicts.json" % key)
    open(infile, "w").write(json.dumps(payload, indent=2))
    open(mapfile, "w").write(json.dumps(id_map, indent=2))
    prompt = R2_PROMPT % (PR, REPO, infile, DIFF, outfile)
    try:
        step(provider, agent, prompt,
             recovery="idempotent", step_id="r2-%s-jury" % key,
             timeout=TIMEOUT, working_directory=REPO, allowed_tools=WRITE_TOOLS)
    except ShimHTTPError as exc:
        if getattr(exc, "status", None) == 409:
            raise
        return key, {}, str(exc)
    except ShimError as exc:
        return key, {}, str(exc)
    verdicts = _read_items(outfile, "verdicts") or []
    ruled = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            continue
        real_id = id_map.get(verdict.get("id"))
        if real_id:
            # Real id restored before anything downstream sees it: merged.json and the
            # arbiter work in real ids, the anonymous ones exist only for the judge.
            ruled[real_id] = dict(verdict, id=real_id)
    # Same reasoning as round 1, and this is where it actually bit: a jury step finished
    # `completed` having written no verdict file, and an empty dict here is exactly what
    # a judge with nothing to judge returns -- so the run reported no failure while a
    # third of the jury never voted. Partial coverage is reported too, without throwing
    # away the verdicts that did arrive.
    if not ruled:
        return key, {}, "judged none of %d findings: no usable verdicts at %s" % (
            len(targets), outfile)
    if len(ruled) < len(targets):
        return key, ruled, "judged only %d of %d findings" % (len(ruled), len(targets))
    return key, ruled, None


with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    _r2 = list(pool.map(_validate, sorted(HARNESSES)))

for finding in deduped:
    finding["verdicts"] = {}

for key, verdicts, err in _r2:
    if err:
        failures["r2-%s" % key] = err
    for finding in deduped:
        if finding["id"] in verdicts:
            finding["verdicts"][key] = verdicts[finding["id"]]

for finding in deduped:
    if finding["corroborated"]:
        finding["verdicts"]["corroboration"] = {
            "verdict": "confirmed",
            # `independent_sources`, not len(sources): the semantic merge adds names to
            # that list, and a claim it grouped is not something the harnesses reported
            # independently. Saying so would manufacture the corroboration.
            "reasoning": "Reported independently by %d of %d harnesses in round 1." % (
                finding["independent_sources"], len(HARNESSES)),
            "corrected_severity": finding["severity"],
        }
    # With two judges per contested finding the jury can disagree, and that disagreement
    # is information the two-harness version could not produce: it marks a finding whose
    # reality is genuinely unsettled, not one that is simply confirmed or simply wrong.
    rulings = {(v or {}).get("verdict") for v in finding["verdicts"].values()}
    if not rulings:
        finding["ruling"] = "unjudged"
    elif len(rulings) == 1:
        finding["ruling"] = rulings.pop()
    else:
        finding["ruling"] = "split"
open(MERGED, "w").write(json.dumps(deduped, indent=2))

# --- stage 3: arbiter ------------------------------------------------------------
ARBITER_PROMPT = (
    "Write the final review for pull request #%d of the repository at %s.\n\n"
    "The adjudicated findings are in the JSON array at %s. Each has `sources` (which "
    "harnesses reported it), `corroborated` (true when two or more reported it "
    "independently, at the same line, in round 1), `merged_semantically` (true when a "
    "merge step judged separate write-ups to be one defect -- one model's opinion, not "
    "independent agreement, with the other write-ups kept under `merged_from`), "
    "`verdicts` keyed by judging harness, and `ruling` "
    "summarising them. A `ruling` of `unjudged` means no harness was eligible to judge "
    "it -- every one of them is among its `sources`, which happens when a merge group "
    "spanned all three. Nobody has validated that finding: read the code yourself, and "
    "read `merged_from` to decide whether those write-ups really are one defect before "
    "you report them as one. Three harnesses "
    "ran: Claude, Codex and OpenCode/DeepSeek. The diff is at %s and PR metadata at %s.\n\n"
    "Resolve the material: drop rejected findings unless the rejection is plainly wrong "
    "(say so if you overrule one), rank what survives by real severity rather than by the "
    "label it carries, and lead with anything that would break in production. Corroborated "
    "findings are the strongest signal in the set -- two or more models found them "
    "independently. A `ruling` of `split` means the two judges disagreed: do not average "
    "them, read the code and say which judge is right and why. Findings marked "
    "needs-context belong in a separate short section, not the main list.\n\n"
    "Write Markdown to %s: a two-sentence verdict on the PR, then the findings as sections "
    "with file:line, what breaks, and the fix. State which harness found each one, and "
    "which judged it. If any "
    "harness or stage failed, say so plainly -- this JSON records it: %s\n\n"
    "Be concise and concrete. Reply with just the path when the file is written."
)
try:
    step("claude_code", "developer",
         ARBITER_PROMPT % (PR, REPO, MERGED, DIFF, META, FINAL, json.dumps(failures)),
         recovery="idempotent", step_id="arbiter", timeout=TIMEOUT,
         working_directory=REPO, allowed_tools=WRITE_TOOLS)
except ShimHTTPError as exc:
    if getattr(exc, "status", None) == 409:
        raise
    failures["arbiter"] = str(exc)
except ShimError as exc:
    failures["arbiter"] = str(exc)

if "arbiter" not in failures and not _report_is_usable(FINAL):
    failures["arbiter"] = "no usable report at %s (%d bytes)" % (
        FINAL, os.path.getsize(FINAL) if os.path.exists(FINAL) else 0)

emit_output({
    "pr": PR,
    "final_review": FINAL,
    "artifacts": ART,
    "round1": {k: len(v) for k, v in round1.items()},
    "after_dedup": len(deduped),
    "corroborated": len([f for f in deduped if f["corroborated"]]),
    "confirmed": len([f for f in deduped if f["ruling"] == "confirmed"]),
    "split": len([f for f in deduped if f["ruling"] == "split"]),
    # Nobody was eligible to judge these: every harness is among their sources. Counted
    # because a run that produces one is not the run the pipeline advertises.
    "unjudged": len([f for f in deduped if f["ruling"] == "unjudged"]),
    "failures": failures,
})

# Any failure, not just the arbiter's. The two report paths used to disagree on this:
# the no-findings one exited non-zero whenever a harness had gone missing, while this
# one returned success as long as the arbiter wrote something -- so identical
# non-delivery in round 1 or round 2 was recorded FAILED or completed depending only on
# whether anybody happened to report a finding. The report is still written and still
# named on the way out; what the exit code says is whether the run happened as designed.
_finish(1 if failures else 0)
