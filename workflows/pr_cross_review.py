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
from the inputs and the run id -- both stable across a resume -- and nothing here reads
the clock or an RNG.
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
# The run this execution belongs to. CAO sets it and keeps it across a resume, so it
# names one review from end to end. Absent only when the script is run by hand.
RUN_ID = os.environ.get("CAO_WORKFLOW_RUN_ID", "") or "no-run-id"

# Outside the checkout, so a review run never dirties the git tree. Keyed by owner as
# well as name -- two repos of the same name from different owners are different repos
# -- and by run, which is what stops two reviews of one PR from deleting each other's
# material. Sharing a directory per PR needed bookkeeping to decide whose files were on
# disk, and that bookkeeping produced a defect in three consecutive reviews: a resume
# wiping the run that superseded it, a resume wiping itself after its checkout had been
# pruned, a refusal that fired on the wrong run. A run that cannot reach another run's
# files needs none of it. The cost is that artifacts accumulate per run; they are small
# beside the bare clone, and nothing else here deletes anything either.
ART = os.path.join(WORKSPACE, ".cao-review", SLUG, "pr-%d" % PR, RUN_ID)
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
                # And from a reviewer that has not already spoken here. One harness
                # filing two defects at one line is ordinary output, not a slip -- a
                # missing bounds check and an ignored error, same line, same category --
                # and collapsing them deleted the second from the run: this path builds
                # no `merged_from`, so its title, detail and scenario reached neither
                # the jury nor the arbiter, and `independent_sources` stayed 1 so
                # nothing downstream could tell. The semantic pass below refuses this
                # grouping too; the automatic one used to do it silently.
                and not set(kept["sources"]) & set(finding["sources"])
            )
            if same_place:
                for src in finding["sources"]:
                    if src not in kept["sources"]:
                        kept["sources"].append(src)
                kept["merged_ids"] = kept.get("merged_ids", []) + [finding["id"]]
                # And its words, not just its id. Same file, same line, same category is
                # where two harnesses agree; whether they are describing one defect is a
                # judgement no arithmetic makes -- and this collapse is what skips round
                # 2, so nothing downstream ever checked it. This path dropped them,
                # leaving the arbiter a corroboration it had no way to verify.
                kept["merged_from"] = kept.get("merged_from", []) + [
                    {k: finding[k] for k in ("id", "sources", "file", "line",
                                             "title", "detail", "failure_scenario")}]
                break
        else:
            deduped.append(finding)
    for finding in deduped:
        # Frozen here, before the semantic pass can union more names into `sources`:
        # this is the flag that skips round 2, and only agreement the harnesses reached
        # on their own -- same defect, same line, no model in between -- may do that.
        finding["independent_sources"] = len(finding["sources"])
        finding["corroborated"] = finding["independent_sources"] > 1
        finding["cluster"] = ""
    return deduped


def _mark_clusters(deduped, groups):
    """Label the findings a merge step judged to be one defect. Nothing is folded away.

    Folding a group into a keeper is what this replaces, and that produced a defect in
    four reviews out of five: the corroboration flag lost, then handed to a claim that
    had not earned it, then the folded write-up's own history dropped, then a group's
    whole outcome decided by which id the model happened to write first, then a second
    group naming an already-folded id skipped without a word. Every one of them was
    bookkeeping in service of deleting material the arbiter then could not read.

    A label deletes nothing. Each finding keeps its author, so each still collects two
    independent verdicts; the arbiter is told which of them one model considers the same
    defect and reports them once if it agrees. Groups that overlap describe one cluster
    between them, which is what the model said -- unless the union would put one
    reviewer's two findings together, the grouping the merge prompt forbids, in which
    case the later group is left out rather than grown into a wrong one.
    """
    by_id = {f["id"]: f for f in deduped}
    clusters = []
    for group in groups:
        if not isinstance(group, list):
            continue
        # Only string ids: `dict.fromkeys` uses each element as a key, so one level of
        # nesting too many -- ordinary enough model output -- used to raise TypeError
        # into module-level code and kill the run after round 1 had been paid for.
        members = [by_id[i] for i in dict.fromkeys(i for i in group if isinstance(i, str))
                   if i in by_id]
        if len(members) < 2:
            continue
        union = {m["id"] for m in members}
        overlapping = [c for c in clusters if c & union]
        for existing in overlapping:
            union |= existing
        authors = [src for i in sorted(union) for src in by_id[i]["sources"]]
        if len(set(authors)) != len(authors):
            continue
        for existing in overlapping:
            clusters.remove(existing)
        clusters.append(union)
    for index, ids in enumerate(sorted(clusters, key=sorted), 1):
        for i in sorted(ids):
            by_id[i]["cluster"] = "c%d" % index
    return deduped


def _jury_targets(deduped, key):
    """The findings `key` is allowed to judge: contested, and none of them its own.

    `sources != [key]` was not that test: it passed a finding back to its own author
    whenever two names were on it, with `JUDGE_FIELDS` stripping the id prefix and
    `sources` and the prompt telling each judge "None of them are yours". `sources` only
    grows past one name in `_dedup` now, where two harnesses really did file at that
    line, so the only findings this excludes are ones the judge had a hand in.
    """
    return [f for f in deduped if not f["corroborated"] and key not in f["sources"]]


def _resume_plan(pinned, diff_exists, meta_exists, worktree_head):
    """What stage 0 has to do: (is this run's own material here, the commit to review).

    A head in the snapshot with the diff and metadata beside it means this run got
    through setup already -- it is being resumed, and it keeps what it pinned, because
    re-reading would put new code under findings already made against the old. Anything
    else is a first execution, which reads the PR as it is now.

    The second value is whether the checkout is already the one to review. A resume
    demands the commit it pinned: another review of this PR may have left the worktree
    elsewhere, and a review of a sibling PR may have pruned it. A first execution never
    accepts what it finds -- an agent with fs_write may have written into the last
    review's copy, and a review starts from the commit its diff describes.

    Inline, this was the region a whole review round could not reach: every predicate
    around it had been extracted and tested, and this one decided whether to re-read.
    """
    mine = bool(pinned.get("head")) and diff_exists and meta_exists
    return mine, bool(mine and worktree_head and worktree_head == pinned["head"])


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
# Plain Python, not an agent step. The PR is read once per run: a resume finds its own
# diff and metadata already on disk and keeps them, which is what stops it re-pointing
# the checkout at commits newer than those describe -- reviewing code that does not
# match the diff is the failure this stage exists to prevent.
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
    # "Already running" was not always true: pruning a finished sibling holds that PR's
    # lock across the `git worktree remove`, so a review starting for it in that
    # sub-second window was refused with a statement about itself that was false.
    raise SystemExit("%s#%d is busy: a review of it is running, or another run is "
                     "clearing its checkout. Try again." % (SLUG_PATH, PR))


def _has_report(directory):
    """Whether some review of that PR has already written its report.

    Any of them: artifacts live one directory per run now, so this asks whether ANY run
    of that PR finished, which is what makes its checkout disposable.
    """
    root = os.path.join(WORKSPACE, ".cao-review", SLUG, directory)
    if not os.path.isdir(root):
        return False
    # The flat path is where reviews landed before artifacts were keyed by run. Volumes
    # carry both, and a PR reviewed under the old layout is just as finished -- reading
    # only the new one would keep its checkout on disk for good.
    return (os.path.exists(os.path.join(root, "final-review.md"))
            or any(os.path.exists(os.path.join(root, run, "final-review.md"))
                   for run in sorted(os.listdir(root))))


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


# Which commit this run reviews. Only this run writes here, so the snapshot answers one
# question: has THIS run already provisioned? If it has -- a resume -- the pin stands
# and the API is not asked again, because re-reading would put new code under findings
# already made against the old, which is the failure this whole stage exists to prevent.
SNAPSHOT = os.path.join(ART, "snapshot.json")


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

# A head in the snapshot with the diff and metadata beside it means this run got through
# setup already: it is being resumed, and it keeps what it pinned. Anything else is a
# first execution, which reads the PR as it is now.
_wt_head = (_git("rev-parse", "HEAD", cwd=WT).stdout.strip()
            if os.path.exists(os.path.join(WT, ".git")) else "")
_mine, _at_head = _resume_plan(_pinned, os.path.exists(DIFF), os.path.exists(META), _wt_head)
HEAD_OID = _pinned["head"] if _mine else _head_oid()

if not _at_head:
    os.makedirs(os.path.dirname(BARE), exist_ok=True)
    os.makedirs(os.path.dirname(WT), exist_ok=True)
    # One lock per repository, held only across the git mutations. Two reviews of
    # different PRs of the same repo otherwise race on one object store and one set of
    # worktree admin files, which fails intermittently rather than cleanly.
    with open(os.path.join(LOCKS, "%s.lock" % SLUG), "w") as _lock:
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
        # A resume checks out the sha it pinned; a first execution takes whatever the
        # fetch just landed, which is not necessarily what the API named a moment ago.
        _git_ok("worktree", "add", "--detach", WT, HEAD_OID if _mine else PR_REF, cwd=BARE)
        _checked_out = _git_ok("rev-parse", "HEAD", cwd=WT).strip()
else:
    _checked_out = HEAD_OID

if not _mine:
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

# Written last, so that a resume recognises a setup that actually finished. Nothing else
# reads it, and no other run can.
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

def _finish(code):
    """Exit, saying on stderr what a failed run's dropped output would have said."""
    sys.stderr.write(_exit_note(code, FINAL, failures))
    raise SystemExit(code)


if len(failures) == len(HARNESSES):
    # Exit non-zero so the run is recorded FAILED. Exiting 0 here would file a run in
    # which nothing was reviewed as `completed`, and `./cao review` would return success.
    # Through `_finish` like every other failing exit: a bare SystemExit(1) prints
    # nothing, and CAO drops the sentinel emit_output just wrote, so a run where all
    # three harnesses came back `completed` having written nothing -- not a step failure,
    # so nothing is journaled either -- was recorded FAILED with the reason nowhere.
    emit_output({"pr": PR, "error": "every harness failed in round 1", "failures": failures})
    _finish(1)


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
            deduped = _mark_clusters(deduped, _groups)
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
# The semantic merge above is a different thing and never sets this flag: one model's
# opinion that two write-ups describe the same defect. It labels them with a shared
# `cluster` and changes nothing else -- both keep their author, both are judged, and the
# arbiter decides whether to report them as one.
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
            #
            # And it states what was observed rather than asserting the conclusion: what
            # %d harnesses agreed on is the place. Whether their write-ups describe one
            # defect is the judgement this verdict used to make on their behalf, and
            # nothing downstream could check it -- so the write-ups travel with it now.
            "reasoning": "%d of %d harnesses independently reported a defect at this "
                         "file and line in round 1, which is why it skipped the jury. "
                         "Their other write-ups are under `merged_from` -- read them and "
                         "confirm they describe the same defect before reporting it as "
                         "one." % (finding["independent_sources"], len(HARNESSES)),
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
    "independently, at the same line, in round 1 -- agreement on the PLACE, so check "
    "`merged_from`, which holds what the other reviewer wrote there, before treating it "
    "as agreement on the claim), `cluster` (a label shared by findings a merge step "
    "judged to describe ONE defect -- one model's opinion, not independent agreement: "
    "read them together, report them once if you agree, and say so if you do not), "
    "`verdicts` keyed by judging harness, and `ruling` "
    "summarising them. A `ruling` of `unjudged` means nothing judged it, which should "
    "not happen while a harness is alive: treat it as unvalidated and read the code. "
    "Three harnesses "
    "ran: Claude, Codex and OpenCode/DeepSeek. The diff is at %s and PR metadata at %s.\n\n"
    "Resolve the material: drop rejected findings unless the rejection is plainly wrong "
    "(say so if you overrule one), rank what survives by real severity rather than by the "
    "label it carries, and lead with anything that would break in production. Corroborated "
    "findings are the strongest signal in the set -- two or more models found a defect at "
    "that line independently -- but they skipped the jury, so you are the only reader who "
    "checks that the write-ups under `merged_from` are really one defect; say so if they "
    "are not. A `ruling` of `split` means the two judges disagreed: do not average "
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
    "clusters": len({f["cluster"] for f in deduped if f["cluster"]}),
    "failures": failures,
})

# Any failure, not just the arbiter's. The two report paths used to disagree on this:
# the no-findings one exited non-zero whenever a harness had gone missing, while this
# one returned success as long as the arbiter wrote something -- so identical
# non-delivery in round 1 or round 2 was recorded FAILED or completed depending only on
# whether anybody happened to report a finding. The report is still written and still
# named on the way out; what the exit code says is whether the run happened as designed.
_finish(1 if failures else 0)
