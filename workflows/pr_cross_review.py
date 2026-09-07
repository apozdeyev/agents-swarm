"""pr_cross_review - three harnesses review a PR, then judge each other's findings.

Round 1 fans out Claude, Codex and OpenCode/DeepSeek over the same diff. Findings are
normalized and deduped, and provenance is kept: a finding two or more harnesses
reported independently is already cross-confirmed and skips round 2. The contested
remainder goes to a full jury -- every harness judges the findings of both others, so
each contested finding collects two independent verdicts and a disagreement between
them is itself a signal. A Claude arbiter writes the final report.

Determinism note: resume re-executes this file top-to-bottom, so every path derives
from the inputs and nothing here reads the clock or an RNG.
"""
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from cao_workflow import ShimError, ShimHTTPError, emit_output, get_inputs, step

INPUTS = {
    "pr": {"type": "int", "required": True},
    "repo_dir": {"type": "path", "required": True},
    "max_workers": {"type": "int", "required": False, "default": 3},
    "step_timeout": {"type": "int", "required": False, "default": 900},
}

_inputs = get_inputs()
PR = int(_inputs["pr"])
REPO = str(_inputs["repo_dir"]).rstrip("/")
MAX_WORKERS = int(_inputs.get("max_workers", 3))
TIMEOUT = float(_inputs.get("step_timeout", 900))

# Outside the repo, so a review run never dirties the git tree.
ART = os.path.join("/home/cao/workspace/.cao-review", os.path.basename(REPO), "pr-%d" % PR)
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

os.makedirs(os.path.join(ART, "round1"), exist_ok=True)
os.makedirs(os.path.join(ART, "round2"), exist_ok=True)


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


def _norm(finding, source, index):
    """Coerce one agent-authored finding into the shape the rest of the script assumes."""
    path = str(finding.get("file") or "").strip()
    if os.path.isabs(path):
        path = os.path.relpath(path, REPO)
    try:
        line = int(finding.get("line"))
    except (TypeError, ValueError):
        line = None
    severity = str(finding.get("severity") or "").lower()
    category = str(finding.get("category") or "").lower()
    return {
        "id": "%s-%d" % (source, index),
        "sources": [source],
        "file": path.lstrip("./"),
        "line": line,
        "severity": severity if severity in SEVERITIES else "medium",
        "category": category if category in CATEGORIES else "correctness",
        "title": str(finding.get("title") or "").strip()[:200],
        "detail": str(finding.get("detail") or "").strip(),
        "failure_scenario": str(finding.get("failure_scenario") or "").strip(),
        "confidence": str(finding.get("confidence") or "medium").lower(),
    }


def _load_round1(path, source):
    data = _read_json(path, {})
    items = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [_norm(f, source, i + 1) for i, f in enumerate(items) if isinstance(f, dict)]


# --- stage 0: fetch the PR -------------------------------------------------------
# Plain Python, not an agent step: gh is cheap, and skipping when the files already
# exist keeps this resume-safe.
if not (os.path.exists(DIFF) and os.path.exists(META)):
    _meta = subprocess.run(
        ["gh", "pr", "view", str(PR), "--json", "title,body,url,headRefName,baseRefName,files"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    _diff = subprocess.run(
        ["gh", "pr", "diff", str(PR)],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    open(META, "w").write(_meta)
    open(DIFF, "w").write(_diff)

FINDING_SHAPE = (
    '{"findings": [{"file": "<path relative to the repo root>", "line": <int>, '
    '"severity": "critical|high|medium|low", '
    '"category": "correctness|security|performance|maintainability|test-coverage", '
    '"title": "<one line, under 120 chars>", '
    '"detail": "<what is wrong and why it matters>", '
    '"failure_scenario": "<concrete inputs or state that produce the wrong result>", '
    '"confidence": "high|medium|low"}]}'
)

R1_PROMPT = (
    "You are reviewing pull request #%d of the repository at %s.\n\n"
    "The unified diff is at %s and the PR metadata (title, body, changed files) is at %s. "
    "Read both. You may read any file in the repository for context.\n\n"
    "Report only defects you can point at in the diff: correctness bugs, security issues, "
    "resource and performance problems, missing test coverage for changed behaviour, and "
    "maintainability problems severe enough to act on. Do not report style preferences, and "
    "do not restate what the code does. Every finding needs a concrete failure scenario -- "
    "if you cannot say what input makes it go wrong, leave it out.\n\n"
    "Write your findings as a single JSON object to %s, with exactly this shape:\n%s\n\n"
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
    return key, _load_round1(out, key), None


with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    _r1 = list(pool.map(_review, sorted(HARNESSES)))

round1 = {key: found for key, found, _ in _r1}
failures = {key: err for key, _, err in _r1 if err}

if len(failures) == len(HARNESSES):
    # Exit non-zero so the run is recorded FAILED. Exiting 0 here would file a run in
    # which nothing was reviewed as `completed`, and `./cao review` would return success.
    emit_output({"pr": PR, "error": "every harness failed in round 1", "failures": failures})
    raise SystemExit(1)

# --- stage 1: normalize and dedup ------------------------------------------------
# Python first: same file, same category, lines within 3 is the same finding.
pool_findings = [f for key in sorted(round1) for f in round1[key]]
deduped = []
for finding in pool_findings:
    for kept in deduped:
        same_place = (
            kept["file"] == finding["file"]
            and kept["category"] == finding["category"]
            and kept["line"] is not None
            and finding["line"] is not None
            and abs(kept["line"] - finding["line"]) <= 3
        )
        if same_place:
            for src in finding["sources"]:
                if src not in kept["sources"]:
                    kept["sources"].append(src)
            kept["merged_ids"] = kept.get("merged_ids", []) + [finding["id"]]
            break
    else:
        deduped.append(finding)

# Then an agent pass for the semantic duplicates Python cannot see: the same bug
# described in different words, at different lines, or filed under a different category.
if len([f for f in deduped if len(f["sources"]) == 1]) > 1:
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
        by_id = {f["id"]: f for f in deduped}
        dropped = set()
        for group in (_read_json(merges_out, {}) or {}).get("merges", []):
            members = [by_id[i] for i in group if i in by_id and i not in dropped]
            if len(members) < 2:
                continue
            keeper = members[0]
            for extra in members[1:]:
                for src in extra["sources"]:
                    if src not in keeper["sources"]:
                        keeper["sources"].append(src)
                keeper["merged_ids"] = keeper.get("merged_ids", []) + [extra["id"]]
                dropped.add(extra["id"])
        deduped = [f for f in deduped if f["id"] not in dropped]
    except ShimHTTPError as exc:
        if getattr(exc, "status", None) == 409:
            raise
        failures["merge-semantic"] = str(exc)
    except ShimError as exc:
        failures["merge-semantic"] = str(exc)

for f in deduped:
    f["sources"] = sorted(f["sources"])
    # Found independently by two or more harnesses: that IS the cross-confirmation round 2
    # exists to produce, so it is confirmed here and skips validation. Two of three
    # agreeing is stronger evidence than two of two was -- the finding had a real chance
    # to go uncorroborated and did not.
    f["corroborated"] = len(f["sources"]) > 1
open(MERGED, "w").write(json.dumps(deduped, indent=2))

if not deduped:
    open(FINAL, "w").write(
        "# PR #%d cross-review\n\nNo findings. Claude, Codex and OpenCode each reviewed "
        "the diff and none reported a defect.\n" % PR)
    emit_output({"pr": PR, "final_review": FINAL, "findings": 0, "failures": failures})
    raise SystemExit(0)

# --- stage 2: cross-validation ---------------------------------------------------
R2_PROMPT = (
    "You are adjudicating code-review findings produced by OTHER reviewers on pull "
    "request #%d of the repository at %s. None of them are yours.\n\n"
    "The findings to judge are in the JSON array at %s. The diff under review is at %s. "
    "Read the actual code before ruling on anything.\n\n"
    "For each finding decide:\n"
    "  confirmed     - the defect is real; the failure scenario holds\n"
    "  rejected      - it is wrong, already handled elsewhere, or not a defect\n"
    "  needs-context - it may be real but cannot be settled from this diff alone\n\n"
    "Judge the claim, not its wording, and do not be deferential: a finding you cannot "
    "reproduce from the code in front of you is not confirmed. Where the severity is "
    "miscalibrated, give a corrected one.\n\n"
    'Write to %s exactly: {"verdicts": [{"id": "<finding id>", '
    '"verdict": "confirmed|rejected|needs-context", "reasoning": "<why, citing the code>", '
    '"corrected_severity": "critical|high|medium|low"}]}\n'
    "Raw JSON only, no code fences. Include every finding. Reply with just the path."
)


def _validate(spec):
    """`key` is the judge; it validates every finding the other harnesses produced.

    Full jury rather than a round-robin: a contested finding is judged by both of the
    harnesses that did not report it, so it ends up with two independent verdicts and
    the judges are free to disagree. One step per judge, not one per (judge, subject)
    pair -- the judge reads all foreign findings at once, which keeps round 2 at
    len(HARNESSES) steps instead of len(HARNESSES) * (len(HARNESSES) - 1).
    """
    key, provider, agent = spec
    targets = [f for f in deduped if not f["corroborated"] and f["sources"] != [key]]
    if not targets:
        return key, {}, None
    infile = os.path.join(ART, "round2", "to-judge-by-%s.json" % key)
    outfile = os.path.join(ART, "round2", "%s-verdicts.json" % key)
    open(infile, "w").write(json.dumps(targets, indent=2))
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
    verdicts = (_read_json(outfile, {}) or {}).get("verdicts", [])
    return key, {v.get("id"): v for v in verdicts if isinstance(v, dict)}, None


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
            "reasoning": "Reported independently by %d of %d harnesses in round 1." % (
                len(finding["sources"]), len(HARNESSES)),
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
    "harnesses reported it), `corroborated` (true when two or more did, independently), "
    "`verdicts` keyed by judging harness, and `ruling` summarising them. Three harnesses "
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

emit_output({
    "pr": PR,
    "final_review": FINAL,
    "artifacts": ART,
    "round1": {k: len(v) for k, v in round1.items()},
    "after_dedup": len(deduped),
    "corroborated": len([f for f in deduped if f["corroborated"]]),
    "confirmed": len([f for f in deduped if f["ruling"] == "confirmed"]),
    "split": len([f for f in deduped if f["ruling"] == "split"]),
    "failures": failures,
})
