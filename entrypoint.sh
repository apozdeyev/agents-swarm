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

# Runs on every start, not just bootstrap: repos cloned since last boot need
# their trust flag too, or the harness exits on launch.
cao-trust /home/cao/workspace /home/cao/workspace/*/

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

  for profile in /opt/cao/profiles/*.md; do
    [ -e "$profile" ] || continue
    echo "[entrypoint] installing $(basename "$profile" .md) (codex)"
    cao install "$profile" --provider codex
  done

  # Written last: any failure above aborts under `set -e`, leaving no sentinel,
  # so the next start retries the whole bootstrap instead of serving a broken install.
  date -u +%FT%TZ > "$SENTINEL"
  echo "[entrypoint] bootstrap complete"
fi

exec cao-server --host "${CAO_BIND_HOST:-0.0.0.0}" --port "${CAO_API_PORT:-9889}"
