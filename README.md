# agents-swarm — CAO in an isolated container

Runs [CAO](https://github.com/awslabs/cli-agent-orchestrator) (`cli-agent-orchestrator`)
in Docker, driving **Claude Code** and **Codex** as harnesses.

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
`developer_codex`, `reviewer_codex` are twins running on **Codex** — CAO stores one
provider per profile name, so a second name is the only way to have both harnesses
live in one session. Delegate by name from the supervisor.

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
