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
./run.sh <repository> [flags]       # same as: python3 claude_sandbox/__main__.py ...
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

## local repositories (`--local`)
For code that isn't on GitHub, or that the sandbox must not reach over the
network, point the launcher at a git repository on this machine:
```
./run.sh --local ~/src/proj [--base BRANCH]   # default base: the checked-out branch
./run.sh --local ~/src/proj --recover         # after a crash: import what was left
```
- **In:** the launcher bundles that one branch (`git bundle`, from its
  current commit; uncommitted changes and other branches stay behind) and
  streams it into the container, which clones it onto the size-capped work
  volume and starts `sbx/<session>/work`. Nothing is bind-mounted: the
  container never sees your repo's `.git` (refs, config, hooks). It gets no
  GitHub key or any other network credential; outbound internet is as usual.
- **Out:** when the session ends, the launcher bundles `sbx/<session>/*` out
  of the still-running container, destroys it, then fetches **only** those
  refs from the bundle file into your repo (fsck on, one explicit refspec per
  branch, never forced: a branch that already exists only fast-forwards). A
  fetch from a bundle runs nothing from the session's repo, and nothing
  outside `refs/heads/sbx/<session>/` can be created or moved. Your checked-out
  branch and work tree are never touched.
- **Review:** a report per branch: commits, provenance (every commit signed
  by the session's key, recorded under `~/.local/state/claude_sandbox/signing-local/`),
  commit-checker errors, files that run with privilege (flagged, not held:
  nothing runs on import), and the session's `PR-Title`/`Not verified` lines.
  Then it's plain git: `git log -p main..sbx/<session>/work`, merge or
  cherry-pick, `git branch -D` when done. There is no CI; running the
  branch's tests runs session code, so treat it like any untrusted branch
  (a `.gitattributes` it adds applies when you check it out).
- **Crashes:** the work volume is kept (`claude-sandbox-keep-*`, never swept)
  until its work is imported. `--recover` mounts each one read-only in a
  throwaway container, exports from there, imports, and deletes the volume.
  With `--volume-size 0`, or a pool without quotas, there is no kept volume
  and a crash loses the session's work (the session is told which case it
  is in). Bundles waiting for import live under
  `~/.local/state/claude_sandbox/local/`.
- GitHub-only flags (`REPOSITORY`, `--account-key`, `--land-all`,
  `--read-only`) are refused with `--local`. Design: `docs/specs/08-local-backend.md`.

## code layout
The launcher is the `claude_sandbox/` package; `run.sh` execs
`claude_sandbox/__main__.py`. Every module, with its role, is listed in
[`claude_sandbox/README.md`](claude_sandbox/README.md). Outside it: `scripts/`
(`sandbox_check.py`, `smoke.sh`, `final-check.sh`), `rulesets/`,
`tests/`, `docs/`, `todo/`.

## tests
```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest
```
CI runs the suite (plus ruff, mypy and shellcheck) on pushes to `main` and
on PRs from branches in this repo, and a real-container smoke test of the whole
launch lifecycle (`tests/integration/test_launch.py`; run it on a host with
`./scripts/smoke.sh`). See `CLAUDE.md` for testing conventions.

## concurrent sessions
Any number of sandboxes can run on one repo at once.
- **Identity.** Each session gets a Docker-style id (`quirky-hopper`, or
  `--name`), used as the container name, in its deploy key's title
  (`ephemeral <host>:<id>`) and in its branch names.
- **Branches.** The clone starts on `sbx/<id>/work` and the session renames it to a numbered slug of its task (`01-fix-x`) before first push, and PRs are opened in alphanumeric branch order; the session is told to use
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
  the file before merge to withdraw it). The filer is `github/issues.py`,
  run by `.github/workflows/file-issues.yml` on every push to `main` (issues
  come from `github-actions[bot]`; the job's only write permission is
  `issues: write`), or by hand in a checkout:
  `python github/issues.py OWNER/REPO --since REV [--dry-run]`. The one
  ledger is a marker on the last line of each issue, so running it twice over
  a range files nothing new. A later edit is ignored, deleting a file closes
  nothing, and a path added again after deletion is a new request. A repo
  without the workflow (and `github/issues.py`) files nothing unless someone
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
must pass a strict allowlist (`github/protect.py` `validate_repo_ruleset`): default
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

**Where protection gaps are reported.** To **you**, never to the session: at
launch (a `warning:` line) and again in the exit summary ("repo warnings"), so
a long session doesn't scroll them away. They are: the default branch isn't
protected, or that couldn't be verified; no status check is required to merge;
and CI jobs that passed on the default branch but aren't required. The
session's CLAUDE.md is identical whatever the state (a test checks it): a list
of a repo's weak points is a map for a prompt-injected session. It only says to
use PRs, never push to the default branch, and tell you before changing a CI
job's name.

**Required status checks.** A CI job only blocks a merge if a ruleset lists it.
To add the ones the launcher named: `gh api repos/OWNER/REPO/rulesets` (note
the ruleset's id), save it with `gh api repos/OWNER/REPO/rulesets/<id> >
ruleset.json`, add this rule to its `rules`, and
`gh api -X PUT repos/OWNER/REPO/rulesets/<id> --input ruleset.json`:
`{"type": "required_status_checks", "parameters":
{"strict_required_status_checks_policy": false, "required_status_checks":
[{"context": "<check name>"}]}}`. Use names that have passed (they must match
the job's displayed name). The launcher offers only static names, never matrix
entries like `test (3.12)`, and warns you (exit summary too) when CI has them. Renaming
a required job strands every open PR, so update the ruleset in step. For a
matrix job, require an aggregator job instead (this repo's `tests-passed`: it
`needs` the others, runs `if: always()` and fails unless all succeeded), so
matrix entries can change freely.

Also:
- Keep repo Actions secrets minimal. A PR from a same-repo branch runs the
  branch's workflow code *with* secrets; put real secrets in an Environment
  restricted to the default branch.
- Not provided: GitHub's merge queue (unavailable on personal-account repos)
  and auto-merge. The shipped rulesets leave "require branches to be up to date"
  off (a PR passes on its own checks, not against the latest default branch);
  turn `strict_required_status_checks_policy` on if you want that (it makes
  concurrent sessions' PRs go stale one after another).
- A session can still force-push or add commits to *another* session's branch,
  which is why the diff, not the branch name, is what you review.

### Secrets: what is scanned, and GitHub push protection
A session can only leak what it can see, and the launcher puts exactly three
secrets in a container: the Claude credential (`sk-ant-...`), the deploy key
and the session signing key (OpenSSH private keys). `portable/untrusted.py` scans
for those, plus GitHub token formats, in commit messages and added lines (CI),
PR text, and `todo/` issue text; it reports the kind, never the value. It does
not scan for credentials the sandbox never holds (AWS, Slack, generic
`password = ...`), and there is no allow marker: a test fixture is assembled
at runtime (`"ghp_" + "x" * 40`).

The launcher does **not rely on** GitHub push protection, but it is the better
first line where you have it, because it stops a known credential before it is
in history. Turn it on with
`gh api -X PATCH repos/OWNER/REPO -f 'security_and_analysis[secret_scanning][status]=enabled' -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled'`
(or Settings -> Code security). Not verified: whether it is available for
private repos on your plan (as of writing it is free for public repos and a
paid GitHub Secret Protection feature for private ones), or whether it applies
to pushes made with a deploy key; test with a throwaway credential before
relying on it.

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
  `./venv/bin/python provenance.py OWNER/REPO --pr N`
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
  success. The launcher offers to make that default read-only (next section).

### Actions hardening: keep session code's CI runs untrusted
The rule: **nothing runs in a trusted execution context before a human has
reviewed it.** A same-repo PR runs the session's own workflow code (pytest, a
`conftest.py`, anything it imports) before anyone reads it; that is fine only
while the run is *untrusted*: a read-only token, no secrets it can reach, no way
to approve or post anything that gates a merge, and logs that stay on GitHub.

**GitHub has no setting that requires approval for same-repo runs.** Its
approval settings (`fork-pr-contributor-approval`,
`fork-pr-workflows-private-repos`) cover *fork* PRs only, and sandbox PRs come
from a branch in the repo. So at launch the launcher checks what *can* be set
(`github/hardening.py`) and states each condition as ok / NOT MET / unknown:
- the default workflow token is read-only, and
- Actions may not approve pull requests: offered on a terminal, one yes/no
  each (default no), showing the exact `PUT .../actions/permissions/workflow`
  it would make; re-running finds nothing left to offer (`--no-hardening-prompt`
  skips the offer, not the check). A 409 (your organisation forbids it) or 403
  (you aren't a repo admin) is reported, never fatal;
- no repo- or organisation-level Actions secrets exist (detected only, names
  never values: it can't move them; put real secrets in an Environment limited
  to the default branch).

What isn't met is repeated as a warning in the exit summary, to you and not to
the session. **Open question, not verified:** whether a deploy key can push a
change under `.github/workflows/`. Removing the `workflow` scope from your `gh`
token (above) doesn't answer it, since a deploy key is a separate SSH credential
with no scopes, and `default_workflow_permissions` is only a default: a workflow
declaring its own `permissions:` gets them. If such a push is accepted, a
session can push an `on: push` workflow that runs at once, before any PR or
hold, with any repo secret it names. That would be a fix-immediately finding
(options: a push ruleset with a `file_path_restriction` on
`.github/workflows/**`, available only on some plans for private repos; keeping
secrets in protected-branch Environments; or a staging repo). The check needs a
scratch repo with no secrets and a deploy key: push a no-op workflow file and
record the result. It has not been done (see `todo/`).

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

## Extra bind mounts

`--mount HOST[:CONTAINER][:ro|rw]` (repeatable) bind-mounts a host directory
into the sandbox. CONTAINER defaults to `/mnt/<name of HOST>`. Mounts are
**read-only unless you add `:rw`**; a read-write mount lets the session change
your host files directly, with no git or PR in between.

A repo can also *request* mounts in `claude_sandbox_host.yaml` (the same
request file as USB, so editing it never rebuilds an image):

```yaml
mounts:
  - source: ~/datasets/reference     # absolute or ~/ path on YOUR machine
    path: /mnt/reference             # optional; default /mnt/<name>
    mode: ro                         # optional; ro (default) or rw
    description: Reference data the tests read   # plain text, <=200
```
Requests are never granted by the repo. Each one is shown at startup with a
`[y/N]` prompt (default no; read-write ones are marked) unless you pass
`--allow-mounts`; without a terminal and without that flag they are not
attached. `--mount` is your own grant and is never prompted. `--no-mounts`
ignores both. A request whose source is missing or whose path is unsafe is
logged and skipped.

Mounts are attached after the deps install and before any credential exists,
and the sandbox `CLAUDE.md` lists each one (path, host source, mode) plus any
request you declined. Refused: `/`, system directories, `/tmp`, the session's
`~/.ssh`, `~/.claude`, `~/.config`, the home directory itself, the work
directory, and overlapping mounts.

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
sandbox (for example this repo's `tests/integration`). The launcher also installs
`incus` and `dnsmasq-base` (even with `--no-deps`, and baked into the cached
deps image), gives root a small subuid/subgid range,
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
check:                      # final gate when you exit Claude, see below
  - name: tests
    path: tools/final-check.sh
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

### final `check:` (a pre-commit hook for the sandbox)
Exiting Claude used to mean teardown at once. Scripts under `check:` (same
entry shape as `setup:`) now run first, in the still-live container, in the
clone, in order, each under its `timeout`, streaming their output to your
terminal. If one fails, you are asked `Resume the Claude session to fix it?
[Y/n]`; yes reopens the same conversation (`claude --continue`, so no new
session and no adopting branches) with a prompt holding each failing script's
last ~6000 characters of output. Claude fixes, commits and pushes (the deploy
key is still valid), you exit, and the check runs again, until it passes or you
say no. Answering no, or having no terminal, tears down as before with a
warning in the log (what was pushed still becomes a PR). Ctrl-C during the
check skips it and tears down. `--no-final-check`, `--no-deps` and `--shell`
skip it.

The launcher adds its own check, whether or not the repo has `check:`
scripts (`claude_sandbox/mergecheck.py`): each of the session's branches is
judged alone against its base and reported if it conflicts with it or is
behind it. GitHub: the session's *pushed* `sbx/<session>/*` branches after a
fetch, each against the repo's default branch (what its PR will target); local
branches never pushed, or moved since, are reported as well. `--local`: the
session's local `sbx/<session>/*` branches against the base it started from.
Empty branches and other sessions' branches are ignored. Findings resume the
session like a failed script; after the rebase, the scripts run again.

This repo's `scripts/final-check.sh` also runs `tests/integration` when the sandbox
was launched with `--nesting` (and says so when it skips them).

Point `check:` at one script that runs the repo's whole lint and test suite
and exits non-zero on any failure. Only the script's *path* comes from the
default branch; its contents come from the session's checkout, so this catches
honest mistakes. It isn't a security boundary: the PR's CI is the real gate.

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
`claude_sandbox/data/sandbox_CLAUDE.md` (use `--claude-md PATH` for a different one). It says the
container and key are thrown away on exit, which repo and branch are reachable,
whether the key can push, whether `sudo` works and which tools exist, and it
tells Claude to keep history linear and to commit and push working steps so
nothing is lost. Git in the container is set to `pull.rebase=true` and
`merge.ff=only` to back this up.

Everything in it is loaded into every session's context, so it is kept short
(a test caps its size) and detail needed only sometimes lives in an on-demand
file: the deps-file schema is rendered from `claude_sandbox/data/sandbox_ref_deps.md` to
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
