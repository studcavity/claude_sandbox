# Reference: `$deps_file` and `setup:` scripts

Read this before creating or editing `$deps_file` or a `setup:` script.

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
check:                     # final gate run when Claude exits (see below); same keys as setup
  - name: tests
    path: tools/final-check.sh
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
  "Setup results this launch" in the Dependencies section of
  `~/.claude/CLAUDE.md`; if one says FAILED, re-run it, read the
  error and tell the user.
- `--no-deps` skips the whole file, `setup:` included.
- A repo script runs automatically with the deploy key present, so treat adding
  or changing one like editing `$deps_file`: security-sensitive, minimal, and
  called out in the commit message and to the user. Pin and verify anything it
  downloads (see how to check a `sha256`, above).

The guard every hazardous `setup:` script needs is in `~/.claude/CLAUDE.md`.

### Final check (`check:`)
Same entry shape as `setup:`, but run **when the user exits Claude**, in this
container, in the clone, before anything is torn down (the session's access is
still live).
Entries run in order, no stdin, each under its `timeout`. If any fails and the
user agrees, the launcher reopens this conversation (`claude --continue`) with
the failing scripts' output tail, so fix it, re-run the script until it
passes, commit (and push, where you can); never weaken the check. Point `check:` at one script
that runs the repo's full lint + test suite and exits non-zero on any failure.
`--no-final-check` and `--no-deps` skip it.
