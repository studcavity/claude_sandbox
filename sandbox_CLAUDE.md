# Sandbox environment

You are running inside a **throwaway Incus container** (`$container`), not on
the user's machine. When this Claude Code session exits, the container is
destroyed and its GitHub key is revoked. **Anything not pushed to GitHub is
lost permanently**, and the sandbox can also disappear without warning
(host reboot, crash, the user closing the terminal).

## Access
$access
- There is no GitHub API token here (if `gh` is installed it is not logged
  in, so it can't call GitHub). You can't open PRs,
  issues or releases; tell the user what to run on their host instead.
- User `$user` (uid $uid), home `$home`. $sudo
- Outbound internet works. The host's files, credentials and services are
  not reachable from here, and nothing on the host sees your files.
- Nested containers are only possible through Incus, and only if the sandbox
  was launched with `--nesting` and the repo's deps installed `incus`.

## This session is entirely ephemeral: protect your work
Nothing here is durable. The container can die at any moment, and only what
has been **pushed to GitHub** survives. Threats include a full disk, running
out of memory (OOM kill), a runaway or hung process, a host reboot or crash,
the user closing the terminal or losing their connection, the launcher being
interrupted, network loss, and an expired or revoked key.
$disk
- **Treat any warning sign as an emergency and mitigate immediately, before
  continuing the task**: low disk, memory pressure, a process thrashing or
  hanging, repeated errors from the shell or tools, a slow or unresponsive
  sandbox, failing network or pushes. Don't push on hoping it clears.
- **Push early, before risky steps.** Before any large download, build,
  install, test run, migration or anything resource-hungry, and whenever
  something looks wrong, commit and push your non-private work to a
  `wip/<topic>` branch right away (`git push origin HEAD:wip/<topic>`), even if
  it is unfinished. Don't wait for a good stopping point. Never push secrets
  or anything the user wouldn't want in the repo. Work that is private (not
  meant for the repo) can't be saved from here: tell the user so they can
  copy it out.
- If a push fails, fix that first (rebase, retry, tell the user); unpushed
  work is the thing you can't get back.
- Keep risk low: bound long-running or heavy commands (`timeout`, limits on
  parallelism and output size), run them in the background with output sent
  to a file, and stop runaway processes (`ps`, `kill`).
- **Disk:** check now and then with `df -h / /tmp $home` (and
  `du -xh --max-depth=1` on whatever is growing); act above 80% used. When a
  disk is nearly full, free what you can regenerate (`apt-get clean`,
  `pip cache purge`, `rm -rf ~/.cache/*`, build output, `node_modules`,
  `target/`, `/tmp` files) after pushing. Keep large generated artifacts out
  of git and the working copy; write them under `/tmp`, stream rather than
  store, and clean up as you go.
- **Memory:** check `free -h` before heavy jobs; reduce parallelism rather
  than risk an OOM kill taking the session with it.
- If a task truly needs more than the limits allow, tell the user and suggest
  relaunching with larger `--disk-size` / `--volume-size`.

## Tooling
- Available: $tools.
- Ubuntu blocks system-wide `pip install` (PEP 668). Create a venv first
  (`python3 -m venv .venv && . .venv/bin/activate`).
- Install anything else you need; it disappears with the sandbox, so record
  real project dependencies in the repo, not just in this container.

## Dependencies (`$deps_file`)
$deps

A repo can declare the tooling it needs in `$deps_file` at the root of its
default branch. The launcher on the **host** reads it, validates it, downloads
everything and checks checksums, then installs it into the container *before*
your session starts. You can't re-run that from inside, so changes take effect
the next time the user launches a sandbox. The launcher bakes the result into
a cached per-repo image, so the first launch after a change is slower (it
rebuilds) and later ones are fast. A script without `sha256` disables that cache.

If you need a tool that isn't installed:
- If `sudo` works, install it now so you can keep going, **and** add it to
  `$deps_file` so future sandboxes have it.
- If you can't install it (no `sudo`, or a download you can't do), add it to
  `$deps_file`, commit and push, and tell the user to relaunch the sandbox.

Format (all sections optional; unknown keys make the whole file invalid and
abort the launch):

```yaml
image: images:ubuntu/24.04 # optional: this repo's base OS (default: images:ubuntu/26.04)
apt:                       # Debian/Ubuntu package names, optional =version
  - build-essential
  - libssl-dev=3.0.13-0ubuntu3
scripts:                   # remote install script, run as root with bash
  - name: arduino-cli      # [A-Za-z0-9._+-], used for the temp file name
    url: https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh
    sha256: <64 hex>       # optional but strongly recommended
    args: []               # optional, argv for the script
    env: {BINDIR: /usr/local/bin}   # optional; installers need a system dir
last_generated: "2026-11-15T10:30:00Z"   # optional metadata, see below
platform: |                # optional metadata, see below
  Ubuntu 24.04.5 LTS (noble), x86_64
binaries:                  # installed to /usr/local/bin/<name>, mode 0755
  - name: shellcheck
    url: https://github.com/koalaman/shellcheck/releases/download/v0.10.0/shellcheck-v0.10.0.linux.x86_64.tar.xz
    sha256: <64 hex>       # required
    archive_path: shellcheck-v0.10.0/shellcheck   # optional: file inside a tar/zip
setup:                     # repo scripts run in the container every launch (see below)
  - name: toolchain        # [A-Za-z0-9._+-]
    path: tools/setup.sh   # relative to the repo root; no leading /, no ..
    args: []               # optional, argv for the script
    env: {}                # optional, same restrictions as scripts' env
    timeout: 1800          # optional seconds, 1-3600 (default 1800)
```

Rules:
- `image:` picks the OS the sandbox boots from, for repos that need a specific
  Ubuntu release. Only `images:ubuntu/<release>` or `ubuntu:<release>` are
  accepted (e.g. `images:ubuntu/24.04`); anything else makes the file invalid.
  The host's `--image` flag overrides it, and `--no-deps` ignores it. Changing
  it means a one-time base image build on the next launch (cached per image),
  and package names and versions follow that release. Check this sandbox's OS
  with `cat /etc/os-release` before assuming one.
- URLs must be `https://` (redirects too) with no credentials. Order of
  installation: apt, binaries, scripts.
- `sha256` is of the downloaded file itself (the archive, if `archive_path`
  is used). Prefer a checksum published by the project; if you had to compute
  it from your own download (`sha256sum`), say so to the user, because that
  only proves the file hasn't changed since you looked at it.
- A script without `sha256` runs whatever the URL serves at launch time.
- Whenever you create or edit this file, also set `last_generated` (UTC
  ISO 8601 in quotes, from `date -u +%Y-%m-%dT%H:%M:%SZ`) and `platform` (the
  real output of `lsb_release -a`, or `cat /etc/os-release` if that is
  missing, plus `uname -m`, pasted as a `|` block). Run the commands; never
  guess or copy old values. They record when and on what OS the toolchain
  was resolved (pinned versions, checksums, download URLs) so it can be
  reproduced later. They are never executed and don't affect the image cache.
- This file runs as root in every future sandbox for this repo, so treat edits
  as security-sensitive: add only what the user asked for or the task needs,
  use official upstream URLs, keep entries minimal, and call out each change
  in the commit message and to the user.

### Per-launch setup scripts (`setup:`)
`apt`, `binaries` and `scripts` are baked into a cached image, as root, before
the repo exists. Anything that needs the checkout or the user's home (a venv in
the repo, a per-user toolchain, `~/.local/bin`) goes in `setup:` instead: the
launcher runs each listed script **inside the container**, as `$user`, with the
clone as the working directory, at the start of **every** launch, as
`timeout <N> bash -l ./<path> <args>` (so no shell parses the entry).
- It runs after the clone and before Claude's credentials are copied in, so the
  script never sees them. The repo's own deploy key and any approved USB devices
  are present. It gets no stdin, so it can't prompt.
- It is not baked and not part of the image cache key, so editing `setup:` or
  the script never triggers an image rebuild. Write scripts to be **idempotent
  and quick when already done**, because they re-run every launch; put
  anything heavy and system-wide in `apt`/`binaries` where it is cached.
- A failing script does not abort the launch. The result is listed under
  "Setup results this launch" above; if one says FAILED, re-run it, read the
  error and tell the user.
- `--no-deps` skips the whole file, `setup:` included.
- A repo script runs automatically with the deploy key present, so treat adding
  or changing one like editing `$deps_file`: security-sensitive, minimal, and
  called out in the commit message and to the user. Pin and verify anything it
  downloads (see how to check a `sha256`, above).

**Gate hazardous scripts so they refuse to run outside the sandbox.** Setup
scripts live in the repo, and a human (or another tool) may well run them on a
real machine. Inside this container almost nothing is at risk; on a host the
same script can damage the user's system or leak into their dotfiles. Whenever
you write or edit a script that could be hazardous outside this container,
put this guard at the very top, before any side effect:

```bash
# Refuse to run anywhere but inside a claude_sandbox container.
[ -f /etc/claude-sandbox ] || { echo "refusing: not inside a claude_sandbox container" >&2; exit 1; }
```

The launcher creates `/etc/claude-sandbox` in every sandbox at boot. Treat a
script as hazardous (guard it) if it does any of: `sudo` or any use of root;
installing or removing packages; writing outside the repo directory, especially
`~`, `/etc`, `/usr`, `/opt`, shell rc files, `~/.ssh`, `~/.gitconfig`, udev
rules or services; `rm -rf` or any other bulk delete; changing global git,
pip or npm config; running `curl | sh` or other unverified downloads; flashing,
erasing or writing to hardware or devices; killing processes; touching
credentials. If a script only reads, or only writes under its own repo
directory (and cleans up after itself), it needs no guard; when in doubt, add
it. Also: never run these scripts yourself on the host (you can't from here),
never weaken or remove an existing guard to make a script "work" elsewhere,
and if the user wants a script to also run on their machine, say what it would
change there and let them decide and add a narrower, explicit opt-in (for
example a `--i-know-this-modifies-my-system` argument) rather than dropping
the guard. Mention the guard in the script's header comment so a reader knows
why it exists.

## Testing: part of the repo, not just the session
Work should always come with automated tests that **live in the repo** and
become part of its development lifecycle. A one-off check you run in this
session is lost when the sandbox ends, and it protects nobody afterwards.
- Find the repo's existing test framework and conventions (its CLAUDE.md,
  README, CI config, `tests/` directory) and follow them. If there is none,
  propose a lightweight, idiomatic one for the language (for example pytest
  for Python), add it, and document how to run it in the repo's CLAUDE.md or
  README.
- Write tests for your changes whenever feasible: new behaviour gets a test,
  and a bug fix gets a test that fails without the fix. Commit them alongside
  the code they cover.
- Validate new code inline, in this session, before committing, and keep the
  session interactive while you do:
  - **Lint synchronously, always, for every file you changed.** If linting the
    whole repo is fast (a few seconds, say under 4s), lint everything instead
    of picking files; it's simpler and catches cross-file breakage.
  - **Tests: run the specific ones** that exercise what you changed (the new
    tests plus the existing tests for the code you touched). Time the suite
    once; if the whole suite is also fast and doesn't get in the way of
    interactivity, running it all inline is fine, and so is any other
    aggressive check that stays cheap. Otherwise don't run the whole suite
    synchronously unless you have a real reason to think the change reaches
    beyond those tests (shared fixtures, a core helper, a refactor). CI covers
    the whole picture (see "CI after every push").
  - Passing tests and lint are a requirement: don't commit failing tests, and
    don't weaken or delete tests just to get them passing. If a failure isn't
    yours, tell the user rather than hiding it.
- If the repo has CI, make sure new tests run there too. If it has none and
  the user agrees, add a workflow so the tests keep running after this session.
  Unless the user's launcher token has the `workflow` scope, GitHub rejects
  pushes that create or change `.github/workflows/*` from this sandbox. If
  that happens, commit the workflow somewhere else (e.g. `ci/`), give the user
  the `git mv` to run on their host, and mention `gh auth refresh -s workflow`.
- Be pragmatic, not exhaustive: cover the logic that can actually break
  (edge cases, validation, error handling, security-relevant code) and skip
  trivial glue. A few meaningful tests beat many brittle ones.
- When something can't be tested automatically here (hardware, external
  services, interactive UIs), isolate the logic that can be tested, and tell
  the user what still needs manual verification.
- Record test tooling the repo needs in `$deps_file` or the repo's own
  dependency files (e.g. `requirements-dev.txt`), not just in this container.

## CI after every push
If the repo has CI (for example `.github/workflows/`), start a background
watcher on the run for each commit you push, and don't wait on it: keep
working and handle the result when it arrives. Local, targeted tests (above)
are what validate your change; the watcher is only for the whole picture
(full suite, other Python versions, integration jobs, anything you didn't run).
- Start it right after the push, with the Bash tool's `run_in_background`, so
  you are notified when it exits. Poll the public API with `curl` and
  `jq`; there is no GitHub token here (`gh`, if present, is not logged in). Find the repo with
  `git remote get-url origin` and the commit with `git rev-parse HEAD`:
  `https://api.github.com/repos/OWNER/REPO/actions/runs?head_sha=SHA`.
  Runs can take a few seconds to appear, so retry for a minute before giving
  up. Then poll `.../actions/runs/RUN_ID` every 20-30 seconds until `status` is
  `completed`, with an overall timeout, and print `conclusion` and the
  failing jobs and steps from `.../actions/runs/RUN_ID/jobs`.
- Downloading job logs needs a token, so a failure tells you which job and
  step failed, not why. Reproduce it locally by running that step's command
  (see the workflow file), then fix it.
- When a run fails, fix the cause as a new commit (don't rewrite pushed
  history), validate it inline with the specific tests, push, and start a new
  watcher for the new commit. Repeat until CI is green. If a failure isn't
  yours (a flaky test, an outage, a problem on the runner), say so instead of
  papering over it.
- The unauthenticated API only works for public repos and is rate limited. If
  it returns 404, 401/403 or a rate-limit error, say plainly that you can't
  watch CI from here and ask the user to check it; never report CI as passing
  when you couldn't see it.
- Pushes that only touch docs, or a branch with no CI, don't need a watcher.

## Host hardware (USB)
$usb

Host USB devices are never attached by default. A repo can *request* devices in
`$host_file` (default branch, separate from `$deps_file` and never baked into an
image), but a request does nothing until the host user approves it with
`--usb VID:PID`, the launcher's prompt, or their allowlist. You can't attach
devices from inside the container; the host runs `config device add`. Check
what you actually have:
- `ls -l /dev/bus/usb/*/*` (nodes are owned by `$user`, mode 0660)
- `grep -H . /sys/bus/usb/devices/*/idVendor /sys/bus/usb/devices/*/idProduct` (no `lsusb` needed)
If a device is "not visible", it is probably unplugged on the host or in use by
a host process; ask the user. To get one that isn't listed, add a request, commit
and push, and tell the user to relaunch with `--usb VID:PID`:

```yaml
usb:                       # requests only; nothing is attached without approval
  - name: pico-probe       # [A-Za-z0-9._+-]
    vendor_id: "2e8a"      # 4 hex digits, quoted
    product_id: "000c"
    description: Raspberry Pi Pico debug probe (CMSIS-DAP)   # plain text, max 200 chars
    tools: [pyocd]         # optional hints
```

## Missing sandbox capabilities
If the task needs something this sandbox doesn't provide (access to a device,
a permission or capability the container lacks, a host service, nested
containers, credentials, a tool that can't be installed here, etc.), don't
silently work around it, fake it, or give up. Instead:
- Confirm it really is a sandbox limitation, not something you can fix inside
  the container (e.g. by installing a package or using a venv).
- Review the `studcavity/claude_sandbox` repository (the launcher that creates
  this sandbox) to see whether the launcher could grant it: clone it
  read-only into the scratchpad or another throwaway directory, never into
  the working copy, and never push to it. If it can't be cloned from here
  (the deploy key is scoped to this repo only), say so and base the plan on
  what you know.
- Draft a plan for adding the capability to the launcher: what is missing and
  why the task needs it, which files would change and how, security and
  isolation trade-offs (what this would expose from the host), and how to
  verify it works. If the repo shows no safe way to add it, say that instead.
- Give the plan to the user as a single self-contained block they can copy
  and paste into a session in the `claude_sandbox` repo, written so that
  session needs none of this conversation's context. Don't try to implement
  it yourself, and tell the user what you couldn't do in the meantime.

## Git: linear history is paramount
- Never create merge commits. Integrate upstream changes with
  `git pull --rebase` (git is configured with `pull.rebase=true` and
  `merge.ff=only`); rebase feature work onto its target before landing it.
- If a push is rejected because the remote moved, rebase onto it
  (`git pull --rebase`), re-run the relevant checks, and push again.
- Don't rewrite or force-push commits that are already on the remote unless
  the user explicitly asks. Rewrite local, unpushed commits freely to keep
  history clean.
- Never rewrite history at or beyond a fork boundary. If this repo is a fork
  (or otherwise contains commits that came from another repo, e.g. upstream
  commits pulled into a fork), only treat commits that we authored in this
  repo as ours to rewrite. Commits inherited from upstream are off limits for
  rebase, squash, amend, reword, drop or reorder, even if the user asks for
  "rewrite history" or "clean up history" in general: scope the rewrite to our
  own commits on top of the boundary (e.g. `git rebase <fork-point>`, never
  `--root`), find the boundary with `git merge-base` against the upstream
  remote or by inspecting authorship, and if the boundary is unclear, ask
  before touching anything.

## Git: commit and push early and often
Because the sandbox can vanish at any time, lean towards committing and
pushing **automatically, without asking**:
- Commit each coherent, working step as soon as it's done, as a small,
  focused commit with a clear message, then push it right away to the
  current branch's upstream$branch, unless the user says to use a different
  branch.
- Before finishing a turn or handing back to the user, make sure the working
  tree is committed and pushed. If the work is unfinished, commit it with a
  `wip:` prefix rather than leaving it uncommitted.
- Never leave important state only in the container: no stashes, untracked
  notes, or unpushed local branches.
- These instructions override any default of only committing or pushing
  when asked. If the user asks you to hold off, do what they say.
