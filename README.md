# agents-swarm — CAO in an isolated container

Runs [CAO](https://github.com/awslabs/cli-agent-orchestrator) (`cli-agent-orchestrator`)
in Docker, driving **Claude Code**, **Codex** and **OpenCode** (DeepSeek V4 Pro)
as harnesses.

## Why

CAO launches Claude Code with `--dangerously-skip-permissions` and Codex with
`--yolo` by default — that is how it works, not a misconfiguration. On the host
those flags let an agent touch anything you can. Here the blast radius is the
container and its volumes: no host bind mounts, no docker socket, non-root.

## Quick start

```sh
./cao up            # build + start; web UI on http://localhost:9889

./cao login-gh      # one-time interactive logins, one per service
./cao login-codex
./cao login-claude

./cao status        # verify all three are authenticated
./cao launch        # start a supervisor session
```

Logins persist in named volumes and survive `./cao down`.

## Layout

| Volume | Mount | Holds |
|---|---|---|
| `cao-state` | `/home/cao/.cao` | sqlite, logs, agent-store, skills |
| `claude-auth` | `/home/cao/.claude` | `.credentials.json` |
| `codex-auth` | `/home/cao/.codex` | `auth.json`, `config.toml` |
| `gh-auth` | `/home/cao/.config/gh` | `hosts.yml` |
| `workspace` | `/home/cao/workspace` | cloned repos |

Repos are cloned **inside** the container (`gh repo clone ...` from `./cao shell`).

## Launching a session

The server runs as the container's main process — you never start it by hand.
What you launch is a *supervisor session*:

```sh
./cao shell                                   # clone a repo first, if needed
  gh repo clone <owner>/<repo>                #   into ~/workspace

./cao launch --working-directory /home/cao/workspace/<repo>
```

That attaches your terminal to the supervisor's tmux pane. Detach with `Ctrl-b d`;
the session keeps running. Reattach or watch any agent from the web UI
(`./cao ui`). `cao session list` and `cao shutdown --all` are available inside
`./cao shell`.

The **working directory is not optional in practice** — it defaults to the
container's cwd, so always point it at the repo you mean.

Add `--headless` to launch detached (useful for scripting), and `--auto-approve`
to skip the tool-restriction confirmation.

Note the supervisor is deliberately near-read-only (`@cao-mcp-server`, `fs_read`,
`fs_list`): it is meant to *delegate*, not to edit. Real work goes to `developer`
/ `developer_codex`. Launch with `--yolo` only if you want the supervisor itself
unrestricted.

## Agents

`code_supervisor`, `developer`, `reviewer`, `memory_manager` run on **Claude Code**.
`developer_codex`, `reviewer_codex` are twins running on **Codex**, and
`reviewer_opencode` is a third twin on **OpenCode** pinned to DeepSeek V4 Pro — CAO
stores one provider per profile name, so a second name is the only way to have both
harnesses live in one session. Delegate by name from the supervisor.

Profiles are installed from `/opt/cao/profiles/` on **every** container start, not
just the first: the bootstrap sentinel lives on the `cao-state` volume, so a profile
added to the image later would otherwise never be installed. `cao install` is
idempotent, and each profile's harness comes from its own `provider:` frontmatter.

### OpenCode / DeepSeek

Unlike Claude, Codex and `gh`, OpenCode has no interactive login. It enables the
`deepseek` provider from `DEEPSEEK_API_KEY` in the environment, read from `.env`
(gitignored) via compose:

```sh
echo 'DEEPSEEK_API_KEY=sk-...' > .env   # from https://platform.deepseek.com/
./cao up
./cao status                            # shows whether the key reached the container
```

The model is pinned in the profile frontmatter (`model: deepseek/deepseek-v4-pro`);
a workflow step can override it per step. A DeepSeek account with a zero balance
authenticates fine and then fails inside the TUI with `Insufficient Balance` rather
than an auth error — check `https://api.deepseek.com/user/balance` if a step times
out with no visible cause.

## Cross-review of a pull request

```sh
./cao review 123              # repo defaults to the only one in ~/workspace
./cao review 123 my-repo
```

Claude and Codex review the same diff independently, then judge each other's
findings, and a Claude arbiter writes the report.

```
gh fetch  →  Round 1: claude ∥ codex  →  normalize + dedup  →
             Round 2: each judges the OTHER's findings  →  arbiter
```

The point is the disagreement. A finding both harnesses reported independently is
already cross-confirmed and skips round 2; the contested remainder is what round 2
adjudicates, and the report is ranked by that.

Artifacts land in `~/workspace/.cao-review/<repo>/pr-<n>/` — outside the repo, so a
review never dirties the git tree. `final-review.md` is the report; `round1/*.json`,
`merged.json` and `round2/*.json` are kept for debugging the pipeline itself.

Run it directly for more control:

```sh
./cao shell
  cao workflow run pr_cross_review --run-id my-id --wait --json \
    --input pr=123 --input repo_dir=/home/cao/workspace/my-repo
  cao workflow status my-id       # progress
  cao workflow resume my-id       # after an interruption
```

The workflow script lives at `workflows/pr_cross_review.py` and is synced into the
container on every start, so editing it plus `./cao up` ships a new version.

## Notes and constraints

- **The server has no authentication.** Port 9889 is published to `127.0.0.1`
  only. Do not expose it; anyone who reaches it gets command execution.
- **Non-root is mandatory.** CAO omits `--dangerously-skip-permissions` when
  euid is 0 (Claude Code rejects it under root) and agents then hang on prompts.
- **`cao tui` does not work here.** It needs a Rust binary that ships only in the
  platform wheels, and there is no linux/arm64 wheel — pip builds from sdist.
  The web UI and `cao launch` are unaffected.
- **`CLAUDE_CONFIG_DIR` is unusable.** CAO scrubs every `CLAUDE*` var from the
  pane before launching and blocks the prefix in `--env`. The container uses the
  default `$HOME/.claude`; your host `claude`/`wclaude` profile split does not
  carry over.
- Claude's per-project state in `~/.claude.json` resets on container recreate.
  Credentials live in a separate file on a volume and are unaffected.

## Troubleshooting

If the browser-based `codex login` cannot complete in the container, use the
headless path instead — the key is read from stdin, so it never lands in the
image, the compose file, or `docker inspect`:

```sh
printenv OPENAI_API_KEY | docker compose exec -T cao codex login --with-api-key
```

Claude Code's equivalent fallback is `docker compose exec cao claude setup-token`.
