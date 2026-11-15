# claude_sandbox

Runs a Claude Code session in a throwaway Incus/LXD container with a
repo-scoped GitHub deploy key. The key and container are destroyed on exit.

## requirements
- `incus`, your user in the `incus-admin` group, incus initialized
- firewall rules for the incus bridge (if you use ufw):
  `sudo ufw allow in on <bridge>`, `sudo ufw route allow in on <bridge>`,
  `sudo ufw route allow out on <bridge>`
- `gh` on the host (every GitHub call goes through it; keep it updated),
  logged in with `gh auth login` as the account you launch as
- **not** the `workflow` scope (`gh auth status` lists scopes). With it the
  sandbox's deploy key can push `.github/workflows/` changes, and an
  `on: push` workflow runs at once with the repo's Actions secrets, before any
  review. The launcher warns at startup if the token has it.

## usage
```
./run.sh <repository> [flags]       # same as: python3 claude_sandbox.py ...
./run.sh --setup-token              # one-time Claude login token
```
`<repository>` is a bare name (looked up under `--github-user`) or
`owner/name`. Defaults: `--github-user studcavity`
(`$CLAUDE_SANDBOX_GITHUB_USER`), `--git-email` the maintainer's noreply address
(`$CLAUDE_SANDBOX_GIT_EMAIL`), `--git-name` the github user, `--cli incus`,
`--image images:ubuntu/26.04`. Without a repository the launcher refuses unless
you pass `--account-key` (an account-wide key can push to every repo you can).
`./run.sh --help` lists every flag.

On every launch the program:
1. creates `./venv` from `requirements.txt` if needed (re-installing only when
   that file changed) and re-runs itself inside it;
2. checks whether this checkout is behind upstream and, on a terminal, offers
   `git pull --ff-only` (skip with `--no-update-check`);
3. checks through `gh` that you are logged in as `--github-user` (skip with
   `--no-user-check`).

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
| `sandbox_ghclient.py` | typed GitHub operations through PyGithub (PR info, status, comments); auth is still your `gh` login |
| `sandbox_claude_config.py` | pre-answers Claude Code's first-run dialogs (theme, onboarding, API key, trust for repos you own) |
| `sandbox_deps.py` | parse/validate/download `claude_sandbox_deps.yaml` |
| `sandbox_host.py` | parse/approve `claude_sandbox_host.yaml` (USB requests) |
| `sandbox_volumes.py` | size-capped volumes over `/tmp` and the clone dir; sweep of leaked ones |
| `sandbox_pool.py` | free-space check (and opt-in growth) of the incus storage pool |
| `sandbox_sessions.py` | session ids (Docker-style names), branch prefix, stale-key reaping, protection check |
| `sandbox_protect.py` | offer and apply the default-branch rulesets (`rulesets/`) |
| `sandbox_todo.py` | stdlib-only: the `todo/<name>.md` follow-up request format (name, size, heading) |
| `sandbox_issues.py` | files an issue for each `todo/` file a commit adds to the default branch (the post-merge filer, run by `file-issues.yml` or by hand) |
| `sandbox_land.py` | host-side: open PRs for a session's branches (labelled, with what the commits say about themselves), hold privileged ones |
| `sandbox_check.py` | stdlib-only commit linter (CI, and inside the sandbox as `sandbox-check`) |
| `sandbox_provenance.py` | per-session commit-signing keys (public half kept on the host) and the `sandbox/provenance` status |
| `sandbox_record.py` | stdlib-only: the Verified / Not verified / Follow-up lines in commit messages, for PR bodies |
| `sandbox_untrusted.py` | stdlib-only: sanitising session text, and the credential scan |
| `sandbox_labels.py` | stdlib-only: the labels on issues and PRs |
| `sandbox_templates.py` | validated `templates/pr.md` and `templates/issue.md` |
| `sandbox_names.py` | stdlib-only: session-name and branch-root rules shared with the checker |
| `sandbox_timing.py` | `--timings`: stages and call counts |
| `sandbox_util.py` | logging, subprocess and signal helpers |
| `sandbox_CLAUDE.md`, `sandbox_ref_deps.md` | templates for the sandbox's `~/.claude/CLAUDE.md` and `~/.claude/ref/deps.md` |

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
Any number of sandboxes can run on one repo at once.
- **Identity.** Each session gets a Docker-style id (`quirky-hopper`, or
  `--name`), used as the container name, in its deploy key's title
  (`ephemeral <host>:<id>`) and in its branch names.
- **Branches.** The clone starts on `sbx/<id>/work`; the session is told to use
  only `sbx/<id>/*`, never the default branch or another session's branches.
- **PRs, opened by the launcher.** When a session ends, the launcher (host,
  your `gh` login) opens a PR for each of its pushed branches. **No GitHub
  credential enters the sandbox**: it can push with its deploy key and nothing
  else. `--no-pr` skips this; `./run.sh REPO --land-all` opens PRs for every
  unmerged `sbx/*` branch (recovery after a crashed launcher; it also grabs
  live sessions' work). Session text is sanitised (no live `@mentions`,
  labelled untrusted).
- **Follow-up issues.** A session adds `todo/<name>.md` (`<name>`: lowercase
  letters, digits, `-`) to a commit to ask for an issue: the first heading is
  the title, the file is the body, the path is its identity. `sandbox_check.py`
  (CI) checks the name, size and heading. The issue is filed **only after the
  file reaches the default branch**, so a human has reviewed the text before it
  is published, and a request whose PR is never merged is never filed (delete
  the file before merge to withdraw it). The filer is `sandbox_issues.py`,
  run by `.github/workflows/file-issues.yml` on every push to `main` (issues
  come from `github-actions[bot]`; the job's only write permission is
  `issues: write`), or by hand in a checkout:
  `python sandbox_issues.py OWNER/REPO --since REV [--dry-run]`. The one
  ledger is a marker on the last line of each issue, so running it twice over
  a range files nothing new. A later edit is ignored, deleting a file closes
  nothing, and a path added again after deletion is a new request. A repo
  without the workflow (and `sandbox_issues.py`) files nothing unless someone
  runs it. The launcher no longer files anything when a session ends, and the
  old `--no-actions`, `--max-actions` and `--max-issues` flags are gone.
- **Exit summary.** The launcher prints every branch with its PR number,
  target, commit count and URL, or why none was opened:
  ```
  ┌ pull requests for session quirky-hopper ───────────────
  │ ✔ opened    #12   sbx/quirky-hopper/1-api → main  [3 commits]
  │             https://github.com/o/r/pull/12
  │ ✔ opened    #13   sbx/quirky-hopper/2-tests → main  [1 commit]
  │             https://github.com/o/r/pull/13
  │ ⚠ HELD      sbx/quirky-hopper/ci
  └ 2 opened. The launcher never merges.
  ```
- **One base.** Every PR targets the default branch. Workflows are filtered
  to PRs into it, so a PR into another branch would get no CI, and the
  launcher no longer opens those. A branch built on another of a session's
  branches shows its parent's commits too; merge the parent first and rebase.
- **Held, not opened:** a branch that changes `.github/`, `.githooks/`,
  `CODEOWNERS`, the deps/host files or a `setup:` script (and any branch
  built on one) gets no PR; review it by hand. In an interactive
  terminal the launcher instead asks `Open a PR for it anyway? [y/N]` (default
  no; non-interactive runs stay held). A branch whose tip can't be pinned or
  that moves while landing is never overridable. Those files run with extra
  privilege (CI with Actions secrets; root and the deploy key in every future
  sandbox), so they are how an injected session would persist.
- **Landing happens after teardown.** The session's key is revoked and its
  container destroyed *before* the launcher checks branches and opens PRs. Each
  PR is pinned to the commit that was checked: if the branch moved anyway
  (another live session's key can push to any branch), the PR is closed and
  reported HELD. What this can't stop: a live sibling pushing to a PR *after* it
  was opened. Deploy keys aren't branch-scoped, so treat every push to an open
  sandbox PR as unreviewed, and don't run concurrent sessions on repos whose CI
  exposes secrets to same-repo PRs.
- **Merging is yours.** The launcher never merges and there is no auto-merge (a
  PR that merges itself would let one injected session change `main`). Use
  GitHub's rebase or squash button to keep history linear.
- **Stale keys.** A launcher killed by SIGKILL or a reboot leaves its write
  deploy key behind. Each launch deletes this host's keys whose container no
  longer exists; siblings' and other hosts' keys are never touched.
- **Image and venv builds** are serialised with file locks, so simultaneous
  first launches build once.

### What actually protects `main`
A deploy key can push to *any* branch, so the branch prefix is advice, not a
permission. The enforcement is GitHub-side. Apply the ruleset once per repo
(needs admin; review it first; it also blocks *you* pushing straight to `main`,
because it has no bypass actors):
```
gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/protect-main.json
```
It requires PRs for the default branch and blocks force-push and deletion.

You don't have to run that by hand: when a launch finds the default branch
doesn't require PRs and you are at a terminal, it asks
`Apply branch protections to this repo? [y/N]` (default no), lists the effects,
and applies the ruleset on a yes. It then offers to also require the CI checks
that **passed on the default branch's latest commit** (names are repo-specific,
and a name that never reports would block every merge, hence opt-in). It won't
duplicate an existing ruleset of that name, and a failure (no admin, plan
without rulesets) only warns. `--no-protect-prompt` silences the offer; the
warning still prints.

**A repo can define its own rulesets** as `rulesets/*.json` on its *default
branch* (reviewed and merged, never a session's branch). If it has any, the
launcher offers those *instead of* the generic one, one yes/no each with the
effects listed first, even when the branch is already protected (GitHub layers
rulesets). Because that file is untrusted text that edits repo settings, it
must pass a strict allowlist (`sandbox_protect.validate_repo_ruleset`): default
branch only, no bypass actors, enforcement `active`/`evaluate`, and only the
rule types `deletion`, `non_fast_forward`,
`pull_request`, `required_status_checks` and `required_signatures`. Invalid
files are skipped with a warning.

This repo ships two more rulesets, applied the same way (launch against the
repo and answer yes, or `gh api -X POST ... --input rulesets/<file>`):
- `require-commit-metadata.json` requires the `commit-metadata` check (see
  `.github/workflows/commit-metadata.yml`), pinned to GitHub Actions
  (`integration_id` 15368) so no other app or token can satisfy it with a
  status of the same name. This is the **one merge gate for the commit
  checker**. It re-runs on every push to the PR and takes its checker from the
  PR's base, so a branch can't weaken the rules it is judged by. To get past a
  wrong failure, use GitHub's own break-glass: an admin bypass, or temporarily
  setting the ruleset's `enforcement` to `disabled` (the audit log records
  it). A repo without the workflow isn't gated by the checker.
- `require-sandbox-provenance.json`: see "Provenance" below.

There used to be a second gate, a host-posted `sandbox/check` status
(`require-sandbox-check.json`). It is gone. If you applied that ruleset, every
PR is now blocked by a context that never reports: delete it.
`gh api repos/OWNER/REPO/rulesets` lists the rulesets (note the id of
`require-sandbox-check`), then
`gh api -X DELETE repos/OWNER/REPO/rulesets/<id>`.

A test keeps every required check name equal to a real CI job name, except
`sandbox/provenance`, which is a commit status.

Each session's CLAUDE.md states this repo's protection status and required
checks, and tells the session to name the checks to require whenever it adds,
renames or removes a CI job (renaming a required job strands open PRs). Also:
- Keep repo Actions secrets minimal. A PR from a same-repo branch runs the
  branch's workflow code *with* secrets; put real secrets in an Environment
  restricted to the default branch.
- Not provided: GitHub's merge queue (unavailable on personal-account repos)
  and auto-merge. Enable "require branches up to date" and use "Update branch"
  when PRs collide.
- A session can still force-push or add commits to *another* session's branch,
  which is why the diff, not the branch name, is what you review.

### Provenance: which commits really came from the session
A deploy key can push to any branch, so a sibling session (or anyone with push
access) can add commits to a session's branch, even after its PR is open. The
launcher can prove which commits a session made:
- At launch the container makes an ed25519 signing key and git signs every
  commit with it. Only the public half leaves; the launcher records it on the
  host (`~/.local/state/claude_sandbox/signing/<owner>/<repo>/<session>.pub`,
  or under `$CLAUDE_SANDBOX_STATE_DIR`), never in the repo, where the session
  could change it. The private half dies with the container. A new session
  never reuses a name that has a recorded key.
- After teardown, for each PR it opened or found, the launcher checks that
  every commit between the default branch and the PR's head has a good
  signature from that session's key **or one of your own SSH signing keys on
  GitHub** (so your rebases and fixups pass if you sign them), and that none is
  a merge commit. It posts the result as the commit status `sandbox/provenance`
  on the head commit (`--no-provenance` skips this).
- A status belongs to one commit: anything pushed later leaves the new head
  without one. With `rulesets/require-sandbox-provenance.json` applied, the
  merge stays blocked until you re-check:
  `./venv/bin/python sandbox_provenance.py OWNER/REPO --pr N`
  (fixes: drop or re-sign foreign commits; or `git rebase --exec 'git commit
  --amend --no-edit -S' origin/main` with your own SSH signing key);
  or break glass for this one head commit with `--override "reason"`, which
  posts success and comments on the PR without checking signatures).
- What it proves: every commit came from this session or from you. What it
  doesn't: that the session's own commits are benign (it holds its key), or
  that CI didn't already run foreign commits (`pull_request: synchronize` runs
  on every push).
- Caveats: GitHub's "Update branch" button and GPG-signed commits fail the
  check (only SSH signing keys are read from your account), which is why the
  ruleset's "require branches to be up to date" is off. A PR from before this
  feature, or from another host, has no recorded key and fails closed. The
  status is posted with your `gh` login, so a ruleset can't pin it to an app:
  anything else allowed to post statuses (e.g. a workflow's `GITHUB_TOKEN` when
  the repo's default workflow permissions allow writes) could post a fake
  success. Set Settings -> Actions -> Workflow permissions to read-only.

### Optional: confine deploy keys to `sbx/` (apply by hand)
A deploy key can create any branch and push tags. A pushed tag runs
tag-triggered (release) workflows on code the session wrote, with whatever
publishing secrets they use; a pushed branch runs any unfiltered `on: push`
workflow. Two rulesets in `rulesets/optional/` stop that for everyone but repo
admins (deploy keys are never admins):
```
gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/optional/sandbox-branches-only.json
gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/optional/no-tag-pushes.json
```
- `sandbox-branches-only`: outside `refs/heads/sbx/**`, non-admins can't
  create, update or delete branches. That includes non-admin collaborators
  merging PRs and bots pushing to e.g. `gh-pages`; skip it if you rely on those.
- `no-tag-pushes`: non-admins can't create, move or delete tags, including
  release bots using `GITHUB_TOKEN`.

They have an admin bypass, which repo-supplied rulesets may not have, so the
launcher never offers them.

## USB passthrough
Sandboxes get no host hardware by default. Two repo files are kept separate on
purpose: `claude_sandbox_deps.yaml` says what to *install* (baked into cached
images) and `claude_sandbox_host.yaml` says what to *borrow from the host* at
launch, so editing the host file never rebuilds an image.

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
Incus attaches *every* host device with that vid:pid, mode 0660 owned by
`ubuntu`. The host must not be using the device, and two sandboxes can't share
one. The sandbox CLAUDE.md lists what the container actually sees (a sysfs
probe). Only pass devices you own and trust: USB firmware can attack its host,
and a debug probe can rewrite whatever it is wired to.

## model
Sessions use `claude-sonnet-5-5` by default. `--model` takes an alias (`opus`,
`sonnet`, `haiku`) or a full model ID; `$CLAUDE_SANDBOX_MODEL` changes the
default for every launch, and `--model default` lets Claude Code pick its own.
It is passed as `claude --model <MODEL>`, so `/model` still works in the
session. Ignored with `--shell`.

## Claude login
Run once to avoid `/login` in every session:
```
./run.sh --setup-token
```
It starts a throwaway sandbox (no GitHub key, no clone) and runs
`claude setup-token`. Sign in through the browser link, then paste the token it
prints at the hidden prompt. It's saved to `~/.config/claude_sandbox/oauth_token`
(mode 600; `--token-file` overrides) and used by every later session. It lasts
about a year; re-run to replace it, or delete the file to go back to `/login`.

Credential lookup, first match wins: `CLAUDE_CODE_OAUTH_TOKEN` in the host
environment, then the token file. `ANTHROPIC_API_KEY` is also passed through if
set. Credentials go into the container over stdin, into a mode-600 file the
session loads and deletes, so they never show in the host's process list.

## cached base image
On first run the launcher builds a local image (`claude-sandbox-<hash>`) with
Claude Code, git, ssh, tmux, Python, build tools and common debugging tools,
and later sandboxes launch from it. The image holds no credentials: SSH keys and
git identity are made per session. It is rebuilt when:
- `--image` or the build steps change (both feed the hash), or you pass
  `--rebuild`;
- it is older than `--hard-max-age` days (default 30). Between `--max-age`
  (default 7) and that, it is still used while a detached process refreshes it,
  so a launch never waits for a routine refresh.

The newest 4 base images (one per OS image in use) are kept; older ones are
deleted after a rebuild. `--no-cache` skips the cache. A ZFS or btrfs storage
pool makes launching a copy-on-write clone instead of an unpack.

## nested containers (`--nesting`)
`--nesting` sets `security.nesting=true` so containers can run inside the
sandbox (for example this repo's `tests/integration`). If the repo's deps
installed `incus`, the launcher also gives root a small subuid/subgid range,
makes the incus socket world-accessible (the sandbox is throwaway and `ubuntu`
already has passwordless sudo) and runs `incus admin init --minimal`. Nesting
loosens the sandbox's isolation from the host a little; use it only for repos
that need it.

## disk space
Every sandbox, image build and cached image shares one incus storage pool. A
default loop-backed pool is small (10GiB), so a few deps images can fill it and
break launches for everyone. The launcher:
- checks the pool before building or launching and stops with the exact
  `incus storage set <pool> size=50GiB` command when under 4GiB is free
  (`--grow-pool SIZE` runs it for you; it changes the host, so it is opt-in);
- caps the root disk of sandboxes and image builds (`--disk-size`, default
  30GiB) and mounts separate capped volumes over `/tmp` and the clone directory
  (`--volume-size`, default 20GiB), so a runaway build fills its own volume.
  They are deleted at exit, and ones leaked by a crashed launcher are swept on
  the next launch. `0` disables either; quotas need a pool driver that supports
  them (btrfs, zfs, lvm);
- keeps at most 4 deps images, and caps the journal at 100MB in the base image.

## repo dependencies
A repo can put a `claude_sandbox_deps.yaml` at the root of its default branch
to have tooling installed in the container before the session starts:
```yaml
image: images:ubuntu/24.04  # optional base OS, see below
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
setup:                      # repo scripts run on every launch, see below
  - name: toolchain
    path: tools/setup.sh
```
Install order is apt, binaries, scripts. `--no-deps` skips all of it. Two
optional metadata keys, `last_generated` (quoted ISO 8601 UTC timestamp) and
`platform` (e.g. `lsb_release -a` plus `uname -m`), record where the toolchain
was resolved so it can be reproduced; they are validated but never executed and
don't affect caching.

**Caching.** The deps are baked into a per-repo local image
(`claude-sandboxdeps-<hash>`, derived from the base image), so only the first
launch after a change pays for the installs. The key covers the base image and
the validated `apt`/`scripts`/`binaries` content (comments, metadata, `image:`
and `setup:` don't count). The image is refreshed on the same `--max-age` /
`--hard-max-age` rules as the base image, and rebuilt when the base was rebuilt
after it or with `--rebuild`; the newest 4 are kept. It is built in a throwaway
container with no credentials, from host-verified downloads. If any script
lacks a `sha256`, the repo is not cached (the script would be frozen at build
time) and everything installs per launch. `--no-cache` disables caching.

### base OS (`image:`)
The default is Ubuntu 26.04 LTS. Only `images:ubuntu/<release>` and
`ubuntu:<release>` (a number or codename) are accepted; anything else, including
other distros, remotes and local images, makes the file invalid and aborts the
launch, so a repo can't point the host at an image it didn't choose.
Precedence: `--image` (the host user may choose anything) over the repo's
`image:` over the default; `--no-deps` ignores the repo's `image:`. Each image
gets its own cached base and deps image, so the first launch on a new release
builds once, and the session is told when `--image` overrode the file.

### per-launch `setup:` scripts
Things that need the checkout or the user's home (a venv in the repo, a
per-user toolchain in `~/.local/bin`) can't be baked into the image. Scripts
listed under `setup:` (`name`, `path`, optional `args`, `env`, `timeout` of
1-3600 seconds) run in the container at the start of **every** launch, as
`timeout <N> bash -l ./<path> <args>` (argv, no shell string), as `ubuntu` in
the clone, after the clone and **before Claude's credentials are copied in**
(the repo's deploy key and approved USB devices are present), with no stdin. A
failure or timeout doesn't abort the launch; it is logged and listed in the
sandbox CLAUDE.md so the session can tell you. `setup:` is not part of the cache
key, so scripts should be idempotent and fast when already done.

Every sandbox gets `/etc/claude-sandbox`, and the sandbox CLAUDE.md tells the
session to put this guard at the top of any script that could be hazardous
outside a sandbox (sudo, package installs, writes under `~` or `/etc`, bulk
deletes, flashing hardware, ...), so it refuses to run on a real machine:
```bash
[ -f /etc/claude-sandbox ] || { echo "refusing: not inside a claude_sandbox container" >&2; exit 1; }
```

Hardening:
- The host fetches the file through the GitHub API and validates it against a
  strict schema (unknown keys, odd names, `-`-prefixed apt specs, loader/shell
  env vars like `LD_PRELOAD`/`PATH` are rejected); any error aborts the launch.
- The host downloads everything (https only, including redirects; no
  credentials in URLs; size caps; the GitHub token is never sent) and checks
  sha256 *before* the bytes enter the container. Binaries must have a checksum.
  Archive members are read in memory, never unpacked.
- Install runs *before* the SSH deploy key and Claude credentials exist in the
  container, so an installer can't read them, and uses argv lists, never a
  shell string.
- Remaining trust: apt repos, and any script without a `sha256` (it runs
  whatever its URL serves). The file and `setup:` scripts run automatically
  (as root, or with the deploy key) in future sandboxes, so review changes to
  them like code.

## sandbox CLAUDE.md
Each session gets `~/.claude/CLAUDE.md` in the container, rendered from
`sandbox_CLAUDE.md` (use `--claude-md PATH` for a different one). It says the
container and key are thrown away on exit, which repo and branch are reachable,
whether the key can push, whether `sudo` works and which tools exist, and it
tells Claude to keep history linear and to commit and push working steps so
nothing is lost. Git in the container is set to `pull.rebase=true` and
`merge.ff=only` to back this up.

Everything in it is loaded into every session's context, so it is kept short
(a test caps its size) and detail needed only sometimes lives in an on-demand
file: the deps-file schema is rendered from `sandbox_ref_deps.md` to
`~/.claude/ref/deps.md`, which the session is told to read before editing
`claude_sandbox_deps.yaml`. Placeholders are `string.Template` fields filled in
by `render_claude_md`; write `$$` for a literal `$`.

## scrollback / terminal behavior
`incus exec` only passes `TERM` into the container, so the launcher also
forwards `COLORTERM`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `LC_TERMINAL` and
`LC_TERMINAL_VERSION` from the host. If scrollback still doesn't work, add
`--tmux` to run the session inside tmux in the container (mouse scrolling,
100k lines of history). If you also use tmux on the host, both use the `Ctrl-b`
prefix: press it twice to reach the inner one. Detaching (`prefix d`) ends the
session and tears down the sandbox.
