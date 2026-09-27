# CAO (cli-agent-orchestrator) in an isolated container.
# Shape adapted from upstream examples/cao-clusters/kubernetes/eks/Dockerfile.
#
# bookworm ships tmux 3.3a, which meets CAO's enforced ">= 3.3" floor,
# so there is no need for the source build that upstream's installer falls back to.
FROM python:3.12-slim-bookworm

ARG CAO_VERSION=2.5.0
ARG CLAUDE_VERSION=2.1.283
ARG CODEX_VERSION=0.153.2
ARG OPENCODE_VERSION=1.18.29
ARG NODE_MAJOR=22

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# procps is required by CAO for provider process detection. ripgrep is here because
# Codex reaches for `rg` unprompted during a review and, without it, burns a search
# round on exit 127 before falling back to find -exec grep.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tmux git curl ca-certificates procps less gnupg ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Node from NodeSource: Claude Code declares engines.node >= 22 and bookworm's
# packaged nodejs is too old. Codex ships a JS entrypoint, so it needs Node too.
RUN curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# gh: used for `gh auth login` inside the container and as the git credential helper.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit \
        "@anthropic-ai/claude-code@${CLAUDE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

# No linux/arm64 wheel is published, so pip falls back to the sdist. That is fine:
# the sdist carries the prebuilt web UI assets. Only the Rust `cao tui` is lost.
RUN pip install --no-cache-dir "cli-agent-orchestrator==${CAO_VERSION}"

# Upstream's v2.5.1 is a git tag only: not on PyPI, and its tree lacks the built web_ui/
# the sdist carries. So the one fix in it this stack needs is applied here instead of
# installing the tag (awslabs/cli-agent-orchestrator bcc73836, #763): codex-cli
# alternates the spinner glyph between "•" and "◦", and a status pattern that knew
# only "•" read a working Codex as finished on every hollow frame -- three of four
# Codex steps torn down mid-turn in one review here. The assert makes the build fail
# rather than silently ship an unpatched image once the line moves upstream; drop
# this step when a PyPI release carries the fix.
RUN python -c 'import cli_agent_orchestrator.providers.codex as m; \
p = m.__file__; s = open(p, encoding="utf-8").read(); \
old, new = "TUI_PROGRESS_PATTERN = r\"•[", "TUI_PROGRESS_PATTERN = r\"[•◦]["; \
assert s.count(old) == 1, "codex.py no longer has the unpatched pattern: re-check whether this step is still needed"; \
open(p, "w", encoding="utf-8").write(s.replace(old, new))'

# Same treatment for Claude Code, whose defect has no upstream fix at all. The idle
# prompt was any ">" or "❯" followed by whitespace, anywhere in the buffer, and once a
# long thinking phase has scrolled the input box's separators out of the window that
# is the only thing COMPLETED still hinges on -- so the echo of the pasted task ("❯ You
# are reviewing…") and even a "=> return" inside Claude's own grep pattern each ended
# a live step, one of them six minutes and 27k tokens in. The real idle prompt is the
# glyph ALONE on its line ("❯\xa0"), so that is what this asks for.
#
# What is NOT established is how much this buys. The live detector runs on the pyte
# COMPOSITE of the pane (CAO_PYTE_STATUS is on), and replaying a saved log offline --
# as the raw stream or fed whole into a fresh screen -- does not reproduce what the
# monitor actually sees: the two replays disagree with each other, and one of them
# predicts hangs that have not happened. So judge it on the runs, not on a replay. In
# the five days after it went in: 46 Claude steps, no step ever ran to its timeout
# (the failure mode to fear here), and teardowns still occurred. Neutral at worst,
# and the misreads it removes are real ones. Bound to CLAUDE_VERSION: a TUI that puts
# placeholder text on the prompt line would never read idle under this, and every
# step would then hang until its timeout -- re-check that before bumping the pin.
RUN python -c 'import cli_agent_orchestrator.providers.claude_code as m; \
p = m.__file__; s = open(p, encoding="utf-8").read(); \
old, new = "IDLE_PROMPT_PATTERN = r\"[>❯][\\s\\xa0]\"", "IDLE_PROMPT_PATTERN = r\"(?m)^[ \\t]*[>❯][ \\t\\xa0]*$\""; \
assert s.count(old) == 1, "claude_code.py no longer has the unpatched pattern: re-check whether this step is still needed"; \
open(p, "w", encoding="utf-8").write(s.replace(old, new))'

RUN useradd --create-home --shell /bin/bash --uid 1000 cao

# A container is always a Claude Code "first run": without this it opens the
# interactive theme picker, cao launch never sees a REPL, and the session dies
# with "Claude Code initialization timed out".
RUN mkdir -p /opt/cao \
    && echo '{"hasCompletedOnboarding": true}' > /opt/cao/claude.json.template \
    && chmod 644 /opt/cao/claude.json.template
COPY profiles/ /opt/cao/profiles/
COPY workflows/ /opt/cao/workflows/
COPY bin/cao-trust /usr/local/bin/cao-trust
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh /usr/local/bin/cao-trust

# Every volume mountpoint must exist in the image owned by cao. Docker seeds an
# empty named volume from the image path including ownership; if the directory is
# missing the volume lands root:root and the non-root user cannot write to it.
RUN mkdir -p /home/cao/.cao /home/cao/.claude /home/cao/.codex \
             /home/cao/.config/gh /home/cao/.aws /home/cao/workspace \
    && chown -R cao:cao /home/cao

# CAO_API_HOST is deliberately NOT set: constants.py uses it both as the server
# bind address and as the base URL the `cao` CLI calls. Setting it to 0.0.0.0
# makes every in-container CLI command send "Host: 0.0.0.0", which
# TrustedHostMiddleware rejects with 400. The bind address goes to cao-server as
# a flag instead (CAO_BIND_HOST), leaving the CLI on its 127.0.0.1 default.
ENV CAO_HOME_DIR=/home/cao/.cao \
    CAO_BIND_HOST=0.0.0.0 \
    CAO_API_PORT=9889 \
    DISABLE_AUTOUPDATER=1

# Never run the (unauthenticated) server as root. Beyond the usual reasons, CAO
# omits --dangerously-skip-permissions when euid==0 because Claude Code rejects
# it under root, and agents then hang on permission prompts.
USER cao
WORKDIR /home/cao/workspace
EXPOSE 9889
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
