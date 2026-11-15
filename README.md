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
- optional: `gh auth refresh -s workflow` so the sandbox can push changes to
  `.github/workflows/` (the launcher warns at startup when it can't; this lets
  sandboxed sessions edit CI, which can read the repo's Actions secrets)

## usage
Edit `github_user` and `git_email` at the top of `run.sh`, then:
```
./run.sh <repository> [extra flags]
```
`run.sh` checks that `gh` is logged in as the expected user, sets up a
`venv` from `requirements.txt`, and runs `claude_sandbox.py` against
`<github_user>/<repository>`. Any extra flags go to `claude_sandbox.py`
(see `python3 claude_sandbox.py --help`).

## code layout
| file | role |
| --- | --- |
| `claude_sandbox.py` | CLI entry point, `Sandbox` lifecycle, pure rendering helpers |
| `sandbox_images.py` | cached base/deps images, applying deps into a container |
| `sandbox_containers.py` | `Container`: thin wrapper over `lxc`/`incus` |
| `sandbox_github.py` | GitHub REST client, token lookup, repo-name validation |
| `sandbox_deps.py` | parse/validate/download `claude_sandbox_deps.yaml` |
| `sandbox_host.py` | parse/approve `claude_sandbox_host.yaml` (USB requests) |
| `sandbox_util.py` | logging, subprocess and signal helpers |
| `sandbox_CLAUDE.md` | template for `~/.claude/CLAUDE.md` inside the sandbox |

## tests
```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```
CI runs the suite (plus pyflakes and shellcheck) on pushes to `main` and on
PRs from branches in this repo. See `CLAUDE.md` for testing conventions.

## USB passthrough
Sandboxes get no host hardware by default. Two separate repo files exist on
purpose: `claude_sandbox_deps.yaml` says what to *install* (baked into cached
images) and `claude_sandbox_host.yaml` says what to *borrow from the host* at
launch. Editing the host file never rebuilds an image, and launchers that
predate it just ignore it.

```yaml
# claude_sandbox_host.yaml
usb:
  - name: pico-probe
    vendor_id: "2e8a"        # 4 hex digits, quoted
    product_id: "000c"
    description: Raspberry Pi Pico debug probe (CMSIS-DAP)  # plain text, <=200
    tools: [pyocd]           # optional hints
```
The file is a **request**; the repo can't grant itself a device. A device is
attached only if you approve it:
- `--usb VID:PID` (repeatable): explicit grant, no repo file needed;
- the interactive `[y/N]` prompt (default no; skipped without a TTY);
- `~/.config/claude_sandbox/usb_allow` (`--usb-allow PATH`): lines of
  `VID:PID` or `VID:PID owner/repo`, attached without a prompt. Must be owned
  by you and not group/world writable.
`--no-usb` ignores everything. Devices are attached after the deps install
(installers never run with hardware present) and before any credential exists.
Incus attaches *every* host device with that vid:pid, with mode 0660 owned by
`ubuntu` (the session has `sudo`, so this is hygiene, not a boundary). The
host must not be using the device, and two sandboxes can't share one.
Sandbox CLAUDE.md lists what was attached, from a sysfs probe of what the
container actually sees. Only pass devices you own and trust: USB firmware can
attack its host, and a debug probe can rewrite whatever it is wired to.
Not verified against real incus/hardware; see `CLAUDE.md` for the smoke run.

## model
Sessions use Sonnet 5.5 (`claude-sonnet-5-5`) by default. Choose another with
`--model` (an alias such as `opus`, `sonnet`, `haiku`, or a full model ID):
```
./run.sh <repository> --model claude-opus-5-5
```
Set `CLAUDE_SANDBOX_MODEL` on the host to change the default for every launch;
`--model` overrides it. `--model default` omits the flag so Claude Code picks
its own default. It's passed as `claude --model <MODEL>`, so you can still
switch with `/model` inside the session. Ignored with `--shell`.

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

## nested containers (`--nesting`)
`--nesting` launches the sandbox with `security.nesting=true`, so containers
can run inside it (for example this repo's `tests/integration`). If the repo's
deps installed `incus`, the launcher also, after the deps install, gives root a
small subuid/subgid range (the default one doesn't fit inside the sandbox's own
id map), makes the incus socket world-accessible (the sandbox is throwaway and
`ubuntu` already has passwordless sudo) and runs `incus admin init --minimal`.
Without `--nesting` nothing changes. Trade-off: nesting loosens the sandbox's
isolation from the host a little, so use it only for repos that need it.

## repo dependencies
A repo can put a `claude_sandbox_deps.yaml` at the root of its default branch
to have tooling installed in the container before the session starts:
```yaml
apt:
  - build-essential
scripts:                    # remote installer, run as root with bash
  - name: arduino-cli
    url: https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh
    sha256: <64 hex>        # optional, recommended
    env: {BINDIR: /usr/local/bin}
binaries:                   # -> /usr/local/bin/<name>
  - name: shellcheck
    url: https://github.com/koalaman/shellcheck/releases/download/v0.10.0/shellcheck-v0.10.0.linux.x86_64.tar.xz
    sha256: <64 hex>        # required
    archive_path: shellcheck-v0.10.0/shellcheck   # optional, file inside a tar/zip
```
Install order is apt, binaries, scripts. Skip all of it with `--no-deps`.

Two optional metadata keys record where the toolchain came from, so it can be
reproduced later: `last_generated` (quoted ISO 8601 UTC timestamp) and
`platform` (text, e.g. the output of `lsb_release -a` plus `uname -m`). They
are validated but never executed and don't affect caching. The sandbox's
CLAUDE.md tells the session to refresh both whenever it edits the file.

Caching: the deps are baked into a per-repo local image
(`claude-sandboxdeps-<hash>`, derived from the base image), so only the first
launch after a change pays for the installs. The key covers the base image and
the validated `apt`/`scripts`/`binaries` content (comments and metadata don't
count). The image is rebuilt when older than `--max-age` days (unpinned apt
packages go stale), when the base image was rebuilt after it, or with
`--rebuild`; the newest 8 are kept. It is built in a throwaway container that
has no credentials, from host-verified downloads. If any script lacks a
`sha256`, that repo is not cached (the script would be frozen at build time)
and everything installs per launch as before. `--no-cache` disables it.

Hardening:
- The host fetches the file through the GitHub API, validates it against a
  strict schema (unknown keys, odd names, `-`-prefixed apt specs, loader/shell
  env vars like `LD_PRELOAD`/`PATH` are rejected) and aborts the launch on any
  error.
- The host downloads everything (https only, including redirects; no
  credentials in URLs; size caps; the GitHub token is never sent) and checks
  sha256 *before* the bytes enter the container. Binaries must have a
  checksum. Archive members are read in memory, never unpacked.
- Install runs *before* the SSH deploy key and Claude credentials exist in the
  container, so an installer can't read them. Commands run with an argv list,
  never through a shell string.
- Remaining trust: apt repos, and any script without a `sha256` (it runs
  whatever its URL serves). A change to this file runs as root in future
  sandboxes, so review it like code.

The sandbox CLAUDE.md documents the format, so a session that can't install
something itself can edit the file, push, and ask you to relaunch.

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
`$tools`, `$branch`, `$deps`, `$deps_file`. Write `$$` for a literal `$`.

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
