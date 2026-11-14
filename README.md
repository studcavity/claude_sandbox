# claude_sandbox

Runs a Claude Code session in a throwaway Incus/LXD container with a
repo-scoped GitHub deploy key. The key and container are destroyed on exit.

## requirements
- `incus`
- membership in the `incus-admin` group
- incus initialized
- these cmds:
```
    sudo ufw allow in on <bridge>
    sudo ufw route allow in on <bridge>
    sudo ufw route allow out on <bridge>
```
- `gh` on host
- authenticated to an account w/ `gh auth login`

## usage
Edit `github_user` and `git_email` at the top of `run.sh`, then:
```
./run.sh <repository> [extra flags]
```
`run.sh` checks that `gh` is logged in as the expected user, sets up a
`venv` from `requirements.txt`, and runs `claude_sandbox.py` against
`<github_user>/<repository>`. Any extra flags go to `claude_sandbox.py`
(see `python3 claude_sandbox.py --help`).

## Claude login
Run this once to avoid `/login` in every session:
```
./run.sh --setup-token
```
It starts a throwaway sandbox (no GitHub key, no clone) and runs
`claude setup-token`. Sign in through the browser link, copy the token it
prints, and paste it at the hidden prompt. It's saved to
`~/.config/claude_sandbox/oauth_token` (mode 600; override with
`--token-file`). Every later session, including concurrent ones, uses it
automatically. The token lasts about a year; re-run the command to replace
it, or delete the file to go back to `/login`.

Credential lookup, first match wins: `CLAUDE_CODE_OAUTH_TOKEN` in the host
environment, then the token file. `ANTHROPIC_API_KEY` is also passed through
if set. Credentials go into the container over stdin, into a mode-600 file
that the session loads and deletes, so they never show up in the host's
process list.

## cached base image
On first run, `claude_sandbox.py` builds a local image (`claude-sandbox-<hash>`)
with Claude Code, git, ssh, tmux, Python (pip, venv), build tools and
common debugging tools (ripgrep, fd, jq, strace, gdb, lsof, ...) already
installed, and later sandboxes launch from it. That skips the install step
on every boot. The image is rebuilt automatically when:
- `--image` or the build steps in the script change (both feed the hash),
- it is older than `--max-age` days (default 7; keeps Claude Code and apt
  packages reasonably fresh — Claude Code also self-updates in the session),
- you pass `--rebuild`.

Old images are deleted after a rebuild. `--no-cache` skips the cache entirely.
Launching from a local image is fastest on a ZFS or btrfs storage pool, where
it's a copy-on-write clone instead of an unpack.

The image contains no credentials: SSH keys and git identity are still
created per session.

## sandbox CLAUDE.md
Each session writes `~/.claude/CLAUDE.md` inside the container. It tells
Claude Code about the sandbox: that the container and its key are thrown away
on exit, which repo and branch it can reach, whether the key can push,
whether `sudo` works, and which tools are installed. It also tells Claude to
keep git history linear (rebase, never merge, never force-push shared
commits) and to commit and push working steps automatically, so nothing is
lost if the sandbox disappears. Git inside the container is set to
`pull.rebase=true` and `merge.ff=only` to back this up.

The text comes from `sandbox_CLAUDE.md`, a Markdown template next to the
script; pass `--claude-md PATH` to use a different one. Placeholders filled
in per session: `$container`, `$access`, `$user`, `$uid`, `$home`, `$sudo`,
`$tools`, `$branch`. Write `$$` for a literal `$`.

## scrollback / terminal behavior
`incus exec` only passes `TERM` into the container, so Claude Code can't tell
which terminal it's running in. `claude_sandbox.py` therefore forwards
`TERM`, `COLORTERM`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `LC_TERMINAL`
and `LC_TERMINAL_VERSION` from the host.

If scrollback still doesn't work, add `--tmux` to run the session inside tmux
in the container (mouse scrolling, 100k lines of history). Notes:
- If you already use tmux on the host, both use the `Ctrl-b` prefix by
  default. Press it twice to reach the inner tmux.
- Detaching (`prefix d`) ends the session and tears down the sandbox.
