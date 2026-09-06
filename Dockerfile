# CAO (cli-agent-orchestrator) in an isolated container.
# Shape adapted from upstream examples/cao-clusters/kubernetes/eks/Dockerfile.
#
# bookworm ships tmux 3.3a, which meets CAO's enforced ">= 3.3" floor,
# so there is no need for the source build that upstream's installer falls back to.
FROM python:3.12-slim-bookworm

ARG CAO_VERSION=2.5.0
ARG CLAUDE_VERSION=2.1.260
ARG CODEX_VERSION=0.153.2
ARG NODE_MAJOR=22

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# procps is required by CAO for provider process detection.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tmux git curl ca-certificates procps less gnupg \
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
    && npm cache clean --force

# No linux/arm64 wheel is published, so pip falls back to the sdist. That is fine:
# the sdist carries the prebuilt web UI assets. Only the Rust `cao tui` is lost.
RUN pip install --no-cache-dir "cli-agent-orchestrator==${CAO_VERSION}"

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
             /home/cao/.config/gh /home/cao/workspace \
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
