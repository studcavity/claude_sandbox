---
type: Guide
title: "Getting started"
description: "Install requirements, launch a first sandbox, and choose the model and Claude login."
tags: [usage, setup]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: cli
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/options.py
    title: Launcher options and defaults
  - id: run
    resource: https://github.com/studcavity/claude_sandbox/blob/main/run.sh
    title: run.sh entry shim
---

# Requirements

- `incus` installed and initialised, with your user in the `incus-admin` group.
- `zstd` on the host (`sudo apt install zstd`). Cached images are published with it:
  measured 2.7x faster to publish than gzip (15s against 40s for a ~700MB image) and
  slightly smaller. Without it the launcher falls back to gzip, so launches still work
  but image builds are slower.
- Firewall rules for the Incus bridge if you use ufw:
  `sudo ufw allow in on <bridge>`, `sudo ufw route allow in on <bridge>`,
  `sudo ufw route allow out on <bridge>`.
- For GitHub sessions, `gh` on the host, logged in (`gh auth login`) as the account
  you launch as. Every GitHub call goes through it, so keep it updated. A `--local`
  session does not use it.
- For GitHub sessions, the `gh` token must **not** have the `workflow` scope (`gh auth status` lists
  scopes). With it, the sandbox's deploy key can push `.github/workflows/`
  changes and an `on: push` workflow runs at once with the repo's Actions
  secrets, before any review. The launcher warns at startup if the token has it.[^cli]

# Launching

The backend is chosen **per launch**, not by the repository: `./run.sh <repository>`
starts a GitHub session (deploy key, PRs) and `./run.sh --local <path>` starts a local
one (no credential, branches imported for review). The same repository can be launched
either way, and the session is told which one it got. Pages in these docs say when they
apply to only one.

```
./run.sh <repository> [flags]    # same as: python3 claude_sandbox/__main__.py ...
./run.sh --setup-token           # one-time Claude login token
./run.sh --help                  # every flag, with its default
```

`<repository>` is a bare name (looked up under `--github-user`) or
`owner/name`. Without a repository the launcher refuses unless you pass
`--account-key`, because an account-wide key can push to every repo you can.
For code that is not on GitHub, see [Local repositories](local-repos.md).
`--help` is the reference for flags and defaults; this documentation only
mentions the ones that need explaining.

On every launch the program:

1. creates `./venv` from `requirements.txt` if needed (reinstalling only when
   that file changed) and re-runs itself inside it;
2. checks whether this checkout is behind upstream and, on a terminal, offers
   `git pull --ff-only` (`--no-update-check` skips it);
3. checks through `gh` that you are logged in as `--github-user`
   (`--no-user-check` skips it).

Then it builds or reuses a [cached image](images-and-caching.md), starts the
container, and hands you a Claude Code session. See
[Concurrent sessions](concurrent-sessions.md) for what happens when you exit.

# Model

Sessions use `claude-sonnet-5-5` by default. `--model` takes an alias (`opus`,
`sonnet`, `haiku`) or a full model ID; `$CLAUDE_SANDBOX_MODEL` changes the
default for every launch, and `--model default` lets Claude Code pick. It is
passed as `claude --model <MODEL>`, so `/model` still works in the session.

# Claude login

Run `./run.sh --setup-token` once to avoid `/login` in every session. It starts
a throwaway sandbox (no GitHub key, no clone) and runs `claude setup-token`.
Sign in through the browser link and paste the token at the hidden prompt. It
is saved to `~/.config/claude_sandbox/oauth_token` (mode 600; `--token-file`
overrides), lasts about a year, and is used by every later session. Re-run to
replace it, or delete the file to go back to `/login`.

Credential lookup, first match wins: `CLAUDE_CODE_OAUTH_TOKEN` in the host
environment, then the token file. `ANTHROPIC_API_KEY` is also passed through if
set. Credentials go into the container over stdin, into a mode-600 file the
session loads and deletes, so they never show in the host's process list.

# Scrollback and terminal behaviour

`incus exec` only passes `TERM`, so the launcher also forwards `COLORTERM`,
`TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `LC_TERMINAL` and
`LC_TERMINAL_VERSION`. If scrollback still does not work, add `--tmux` to run
the session inside tmux in the container (mouse scrolling, 100k lines of
history). If you also use tmux on the host, both use the `Ctrl-b` prefix:
press it twice to reach the inner one. Detaching (`prefix d`) ends the session
and tears down the sandbox.

[^cli]: Launcher options and defaults
