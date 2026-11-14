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
