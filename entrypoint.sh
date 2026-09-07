#!/usr/bin/env bash
set -euo pipefail

: "${CAO_HOME_DIR:=/home/cao/.cao}"
export CAO_HOME_DIR

# ~/.claude.json is on the container filesystem, not a volume, so it is gone after
# every container recreate. Without the onboarding flag Claude Code opens its
# interactive theme picker and cao launch fails with an init timeout.
if [ ! -f "$HOME/.claude.json" ]; then
  install -m 600 /opt/cao/claude.json.template "$HOME/.claude.json"
fi

# Guard on our own sentinel, NOT on anything cao creates: `cao init` makes db/
# itself, so a db-existence guard would mark bootstrap done before the profile
# installs ran and mask the failure forever on the next restart.
# ~/.gitconfig is not on a volume, so re-apply on every start.
if [ -n "${GIT_USER_NAME:-}" ] && [ -n "${GIT_USER_EMAIL:-}" ]; then
  git config --global user.name "$GIT_USER_NAME"
  git config --global user.email "$GIT_USER_EMAIL"
fi

# WORKFLOW_SPEC_DIR is $CAO_HOME_DIR/workflows, which lives on the state volume.
# Sync from the image on every start so a rebuild ships new workflow versions.
mkdir -p "$CAO_HOME_DIR/workflows"
for wf in /opt/cao/workflows/*.py; do
  [ -e "$wf" ] || continue
  cp -f "$wf" "$CAO_HOME_DIR/workflows/"
done

# Runs on every start, not just bootstrap: repos cloned since last boot need
# their trust flag too, or the harness exits on launch.
cao-trust /home/cao/workspace /home/cao/workspace/*/

# Same reasoning as the workflow sync above, and NOT inside the bootstrap guard:
# the sentinel survives on cao-state, so a profile added to the image after the
# first boot would never be installed. `cao install` is idempotent (re-installing
# an existing profile exits 0 and overwrites), so re-running it every start is safe.
# Profiles no longer share one provider (codex twins plus the opencode twin), so read
# it from each file's frontmatter. A profile with no provider: is a bug, not a default
# -- fail loudly rather than silently installing it against the wrong harness.
for profile in /opt/cao/profiles/*.md; do
  [ -e "$profile" ] || continue
  prov=$(sed -n 's/^provider:[[:space:]]*\([A-Za-z_][A-Za-z_0-9]*\).*/\1/p' "$profile" | head -1)
  if [ -z "$prov" ]; then
    echo "[entrypoint] $profile has no 'provider:' in frontmatter" >&2
    exit 1
  fi
  echo "[entrypoint] installing $(basename "$profile" .md) ($prov)"
  cao install "$profile" --provider "$prov"
done

# OpenCode enforces a workspace boundary of its own, entirely separate from CAO's
# allowed_tools: CAO translates allowed_tools into the agent's `permission:` frontmatter,
# which carries no `external_directory` key, so OpenCode falls back to its default of
# prompting. Nothing answers that prompt from a workflow step, so the step burns its whole
# timeout and fails with a 504 and no visible cause.
#
# Review artifacts live outside the repo by design, and agents also invent their own
# scratch paths under /tmp, so a targeted allowlist cannot cover this -- every miss costs a
# full step timeout. Allow the lot. This is not a loosening: CAO already runs Claude with
# --dangerously-skip-permissions and Codex with --yolo, and the security boundary is the
# container, which has no host bind mounts and runs non-root.
#
# Written after the installs because `cao install` creates the file. CAO's helper is
# read-modify-write and preserves top-level keys it does not own.
python3 - <<'OCPERM'
import json
import os

path = "/home/cao/.aws/opencode/opencode.json"
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {"$schema": "https://opencode.ai/config.json"}
if os.path.exists(path):
    cfg = json.load(open(path))
cfg.setdefault("permission", {})["external_directory"] = {"*": "allow"}
with open(path, "w") as fh:
    json.dump(cfg, fh, indent=2)
OCPERM

SENTINEL="$CAO_HOME_DIR/.bootstrap-complete"

if [ ! -f "$SENTINEL" ]; then
  echo "[entrypoint] bootstrapping CAO state in $CAO_HOME_DIR"
  cao init

  # `cao install` takes exactly one AGENT_SOURCE — it is not variadic.
  # --provider is honoured for the install but NOT persisted into the stored
  # profile, and `cao launch` falls back to kiro_cli (not installed here) when a
  # profile names no provider. So stamp it in explicitly, the way the codex
  # twins carry theirs, and the agent works however it is launched.
  for agent in code_supervisor developer reviewer memory_manager; do
    echo "[entrypoint] installing $agent (claude_code)"
    cao install "$agent" --provider claude_code
    python3 - "$CAO_HOME_DIR/agent-context/$agent.md" <<'PY'
import re, sys
path = sys.argv[1]
text = open(path).read()
if not re.search(r'^provider:', text, re.M):
    text = re.sub(r'^(name: .*)$', r'\1\nprovider: claude_code', text, count=1, flags=re.M)
    open(path, 'w').write(text)
PY
  done

  # Written last: any failure above aborts under `set -e`, leaving no sentinel,
  # so the next start retries the whole bootstrap instead of serving a broken install.
  date -u +%FT%TZ > "$SENTINEL"
  echo "[entrypoint] bootstrap complete"
fi

exec cao-server --host "${CAO_BIND_HOST:-0.0.0.0}" --port "${CAO_API_PORT:-9889}"
