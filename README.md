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
- `gh` on host (every GitHub call goes through it; keep it updated)
- authenticated to the account you launch as, with `gh auth login`
- optional: `gh auth refresh -s workflow` so the sandbox can push changes to
  `.github/workflows/` (the launcher warns at startup when it can't; this lets
  sandboxed sessions edit CI, which can read the repo's Actions secrets)

## usage
```
./run.sh <repository> [flags]       # same as: python3 claude_sandbox.py ...
./run.sh --setup-token              # one-time Claude login token
```
`run.sh` is only a shim that `exec`s `claude_sandbox.py`; everything below is
the program. `<repository>` is a bare name (looked up under `--github-user`) or
`owner/name`. Defaults: `--github-user studcavity` (`$CLAUDE_SANDBOX_GITHUB_USER`),
`--git-email` the maintainer's noreply address (`$CLAUDE_SANDBOX_GIT_EMAIL`),
`--git-name` the github user, `--cli incus`, and `--image images:ubuntu/26.04`
(Ubuntu 26.04 LTS; a repo can choose another release, see "base OS" below).
Running without a repository is refused unless you pass `--account-key`
(an account-wide key can push to every repo you can).

On every launch the program:
1. creates `./venv` from `requirements.txt` if needed (re-installing only when
   that file changed) and re-runs itself inside it;
2. checks whether this checkout is behind its upstream. If so it lists the new
   commits and, on a terminal, asks `git pull now? [y/N]` (`git pull --ff-only`,
   then restarts itself; a failed or declined pull just continues; without a
   terminal it only prints the notice). Skip with `--no-update-check`;
3. checks, through `gh`, that you are logged in as `--github-user` and exits
   otherwise (skip with `--no-user-check`; not done for `--setup-token`).

## code layout
| file | role |
| --- | --- |
| `run.sh` | thin shim: `exec`s `claude_sandbox.py` |
| `claude_sandbox.py` | CLI entry point and defaults, `Sandbox` lifecycle, pure rendering helpers |
| `sandbox_bootstrap.py` | stdlib-only: create `venv/`, install requirements, re-exec |
| `sandbox_update.py` | offer to `git pull` when this checkout is behind |
| `sandbox_images.py` | cached base/deps images, applying deps into a container |
| `sandbox_containers.py` | `Container`: thin wrapper over `incus` (or `lxc`) |
| `sandbox_github.py` | GitHub access via the `gh` CLI (invoke), repo-name validation, concurrent prefetch of a launch's independent reads |
| `sandbox_ghclient.py` | typed GitHub operations through PyGithub (PR info, status, comments, labels); auth is still your `gh` login |
| `sandbox_claude_config.py` | pre-answers Claude Code's first-run dialogs (theme, onboarding, API key, trust for repos you own) |
| `sandbox_deps.py` | parse/validate/download `claude_sandbox_deps.yaml` |
| `sandbox_host.py` | parse/approve `claude_sandbox_host.yaml` (USB requests) |
| `sandbox_volumes.py` | size-capped volumes over `/tmp` and the clone dir; sweep of leaked ones |
| `sandbox_pool.py` | free-space check (and opt-in growth) of the incus storage pool |
| `sandbox_sessions.py` | session ids (Docker-style names), branch prefix, stale-key reaping, protection check |
| `sandbox_actions.py` | host-side: `Sandbox-Action:` commit trailers (versioned JSON) become issues etc.; spec in the docstring |
| `sandbox_land.py` | host-side: open PRs for a session's branches (labelled, with what the commits say about themselves), hold privileged ones |
| `sandbox_check.py` | stdlib-only commit linter (CI, and inside the sandbox as `sandbox-check`); `--post-status` (the `sandbox/check` merge gate, `--override` to break glass), `--file-issues` (post-merge filer) |
| `sandbox_record.py` | stdlib-only: the Verified / Not verified / Follow-up lines in commit messages, for PR bodies |
| `sandbox_untrusted.py` | stdlib-only: sanitising session text, and the credential scan |
| `sandbox_labels.py` | stdlib-only: the labels on issues and PRs |
| `sandbox_templates.py` | validated `templates/pr.md` and `templates/issue.md` |
| `sandbox_names.py` | stdlib-only: session-name and branch-root rules shared with the checker |
| `sandbox_timing.py` | `--timings`: stages and call counts |
| `sandbox_util.py` | logging, subprocess and signal helpers |
| `sandbox_CLAUDE.md` | template for `~/.claude/CLAUDE.md` inside the sandbox |

## tests
```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```
CI runs the suite (plus pyflakes, mypy and shellcheck) on pushes to `main` and
on PRs from branches in this repo, and a real-container smoke test of the whole
launch lifecycle (`tests/integration/test_launch.py`; run it on a host with
`./smoke.sh`). See `CLAUDE.md` for testing conventions.

## concurrent sessions
Any number of sandboxes can run on one repo at once. What keeps them apart:
- **Identity.** Each session gets a Docker-style id (`quirky-hopper`; or
  `--name`), used as the container name, in its deploy key's title
  (`ephemeral <host>:<id>`) and in its branch names.
- **Branches.** The clone starts on `sbx/<id>/work`; the session is told to use
  only `sbx/<id>/*`, never the default branch or another session's branches.
- **PRs, opened by the launcher.** When a session ends, the launcher (host,
  your `gh` login) opens a PR for each of its pushed branches. **No GitHub
  credential enters the sandbox**: it can push with its deploy key and nothing
  else, so it can't open, approve or merge PRs. `--no-pr` skips this;
  `./run.sh REPO --land-all` opens PRs for every unmerged `sbx/*` branch
  (recovery after a crashed launcher; it also grabs live sessions' work).
  Text from the session is sanitised (no live `@mentions`, labelled untrusted).
- **Follow-up actions.** A commit message can end with
  `Sandbox-Action: {"v": 1, "type": "issue", "id": "<slug>-<YYYYMMDD>-<session>-<4 random>", "title": "..."}`
  trailers (plus `Sandbox-Session: <session>` on every commit, and a
  `todo/<id>.md` brief in the same commit; `sandbox_check.py` verifies all
  three and is meant to run in CI on PRs). After the PRs, the launcher files an issue per unaddressed item
  and comments on the commit with the link (that comment is also the
  idempotency record, so re-running never duplicates). Messages that don't
  match the spec are ignored. New action types are new `(type, version)`
  handlers; see `sandbox_actions.py`. `--no-actions` skips this.
- **Exit summary.** The launcher prints a block listing every branch with its
  PR number, target, commit count and URL (or why none was opened):
  ```
  ┌ pull requests for session quirky-hopper ───────────────
  │ ✔ opened    #12   sbx/quirky-hopper/1-api → main  [3 commits]
  │             https://github.com/o/r/pull/12
  │ ✔ opened    #13   sbx/quirky-hopper/2-tests → sbx/quirky-hopper/1-api (stacked)  [1 commit]
  │ ⚠ HELD      sbx/quirky-hopper/ci
  └ 2 opened. Merge stacked PRs bottom-up (parents first). The launcher never merges.
  ```
- **Stacked PRs.** For iterative work a session may build each branch on its
  own previous one. The launcher targets each PR at the closest of that
  session's own branches it contains, so the diff shows only that step, and
  never stacks on another session's branch. The privileged-file hold is
  judged against the default branch, so a branch above a held one is held too.
  Merge bottom-up; after a squash/rebase merge the next branch needs
  `git rebase --onto origin/HEAD <old-parent-tip>`. PRs into a non-default
  base may not trigger CI that is filtered to the default branch.
- **Held, not opened:** a branch that changes `.github/`, `.githooks/`,
  `CODEOWNERS`, the deps/host files or a `setup:` script gets no PR; the
  launcher says so and you review the branch by hand. Those files run with
  extra privilege (CI with Actions secrets; root and the deploy key in every
  future sandbox), so they are how an injected session would persist.
- **Merging is yours.** The launcher never merges and there is no auto-merge:
  a PR that merges itself would let one injected session change `main`.
  Merge each PR with GitHub's rebase or squash button (history stays linear).
- **Stale keys.** A launcher killed by SIGKILL or a reboot leaves its write
  deploy key behind. Each launch deletes this host's keys whose container no
  longer exists; live siblings' keys and other hosts' keys are never touched.
- **Image and venv builds** are serialised with file locks, so simultaneous
  first launches build once instead of racing.

### What actually protects `main`
A deploy key can push to *any* branch, so the branch prefix is advice, not a
permission. The enforcement is GitHub-side. Apply the ruleset once per repo
(needs admin; review it first; it also blocks *you* pushing straight to
`main`, because it has no bypass actors):
```
gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/protect-main.json
```
It requires PRs for the default branch and blocks force-push, deletion and
merge commits.

You don't have to run that by hand: when a launch finds the default branch
doesn't require PRs, and you are at a terminal, it asks
`Apply branch protections to this repo? [y/N]` (default no), lists the effects,
and applies the ruleset on a yes. It then offers to also require the CI checks
that **passed on the default branch's latest commit** (names are repo-specific,
so they aren't in the file; a name that never reports would block every
merge, hence opt-in). It won't create a duplicate if a ruleset of that name
exists, and a failure (no admin, plan without rulesets) only warns.
`--no-protect-prompt` silences the offer; the warning still prints.

**A repo can define its own rulesets**, in the same place: `rulesets/*.json` on
its *default branch* (reviewed and merged, never a session's branch). If it
has any, the launcher offers those *instead of* the generic one above, one
yes/no each, listing the effects first, and it does so even when the branch is
already protected (a repo-defined ruleset may just add a rule: GitHub layers
rulesets). Because that file is untrusted text that edits repo settings, it
must pass a strict allowlist (`sandbox_protect.validate_repo_ruleset`):
default branch only, no bypass actors, enforcement `active`/`evaluate`, only
the rule types `deletion`, `non_fast_forward`, `required_linear_history`,
`pull_request`, `required_status_checks` and `required_signatures`. Invalid
files are skipped with a warning. This repo ships
`rulesets/require-commit-metadata.json` (requires the `commit-metadata` check,
see `.github/workflows/commit-metadata.yml`); apply it to a repo that already
has `protect-default-branch` with
`gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/require-commit-metadata.json`
or just launch against it and answer yes. A test keeps every required check
name equal to a real CI job name, except `sandbox/check`, which is a commit
*status* posted by `sandbox_check.py --post-status OWNER/REPO --pr N` (no
workflow needed). `rulesets/require-sandbox-check.json` requires it.
**Don't apply that ruleset yet**: nothing posts the status automatically until
the launcher does it at session exit (plan 5d), so every PR would sit blocked
until someone ran the command by hand. The break-glass levels (re-run,
`--override "reason"`, suspend the ruleset) are in `docs/plans/README.md`.

Each
session's CLAUDE.md states this repo's protection status and which checks are
required, and tells the session to name the check names to require whenever it
adds, renames or removes a CI job (renaming a required job strands open PRs).
Also:
- Keep repo Actions secrets minimal. A PR from a same-repo branch runs the
  branch's workflow code *with* secrets; put real secrets in an Environment
  restricted to the default branch.
- Not provided: GitHub's merge queue (unavailable on personal-account repos)
  and auto-merge. Without a queue, enable "require branches up to date" and
  use the "Update branch" button when PRs collide.
- A session can still force-push or add commits to *another* session's branch,
  which is why the diff, not the branch name, is what you review.

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

The newest 4 base images are kept (one per OS image in use) and older ones are
deleted after a rebuild. `--no-cache` skips the cache entirely.
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

## disk space
Every sandbox, image build and cached image shares one incus storage pool. A
default loop-backed pool is small (10GiB), so a few deps images can fill it and
break launches for everyone; per-container limits can't help then. The launcher:
- checks the pool before building or launching and stops with the exact
  `incus storage set <pool> size=50GiB` command when under 4GiB is free
  (`--grow-pool SIZE` runs it for you; it changes the host, so it is opt-in);
- caps the root disk of sandboxes and image builds (`--disk-size`, default
  30GiB) and mounts separate capped volumes over `/tmp` and the clone
  directory (`--volume-size`, default 20GiB), so a runaway build fills its own
  volume rather than the OS. They are deleted at exit, and volumes leaked by a
  crashed launcher are swept on the next launch. `0` disables either;
  quotas need a pool driver that supports them (btrfs, zfs, lvm);
- keeps at most 4 deps images, and caps the journal at 100MB in the base image.
The sandbox CLAUDE.md tells Claude the session is ephemeral and to free space
and push a `wip/` branch immediately when a disk fills.

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

### base OS (`image:`)
Sandboxes boot from Ubuntu 26.04 LTS (`images:ubuntu/26.04`) by default. A repo
that needs another release says so in its deps file:
```yaml
image: images:ubuntu/24.04
```
Only `images:ubuntu/<release>` and `ubuntu:<release>` (a number or codename)
are accepted; anything else, including other distros, other remotes and local
images, makes the file invalid and aborts the launch, so a repo can't point the
host at an image it didn't choose. Precedence: `--image` (the host user may
choose anything) over the repo's `image:` over the default. `--no-deps` ignores
the repo's `image:`. The file is read before the base image is built, each
image gets its own cached base and deps image (the first launch on a new
release builds once), and the session is told when `--image` overrode the file.
`image:` is not part of the install cache key.

### per-launch `setup:` scripts
Things that need the checkout or the user's home (a venv in the repo, a
per-user toolchain in `~/.local/bin`) can't be baked into the image. List the
repo's own scripts under `setup:` and the launcher runs them in the container
at the start of **every** launch:
```yaml
setup:
  - name: lightning-bugs
    path: lightning_bugs/tools/setup.sh   # repo-relative; no leading /, no ..
    args: []                              # optional
    env: {}                               # optional (same rules as scripts)
    timeout: 1800                         # optional, 1-3600 seconds
```
Each runs as `timeout <N> bash -l ./<path> <args>` (argv, no shell string) as
the `ubuntu` user in the clone, after the clone and **before Claude's
credentials are copied in** (the repo's deploy key and approved USB devices are
present), with no stdin. A failure or timeout doesn't abort the launch; it is
logged and listed in the sandbox CLAUDE.md so the session can tell you.
`setup:` is not part of the cache key, so editing it never rebuilds an image;
scripts should be idempotent and fast when already done. `--no-deps` skips it.
The path is validated on the host, but the script itself is repo code that
runs automatically, so review changes to it like changes to this file. Older
launchers reject a deps file containing `setup:`.

Every sandbox gets `/etc/claude-sandbox`. The sandbox CLAUDE.md tells the
session to put a guard at the top of any script that could be hazardous
outside a sandbox (sudo, package installs, writes under `~` or `/etc`, bulk
deletes, flashing hardware, ...), so it refuses to run on a real machine:
```bash
[ -f /etc/claude-sandbox ] || { echo "refusing: not inside a claude_sandbox container" >&2; exit 1; }
```
For example, Vape-Hacks' `lightning_bugs/tools/setup.sh` writes into
`~/.local/bin` and `~/.arduino15`, so it should start with that guard.

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
