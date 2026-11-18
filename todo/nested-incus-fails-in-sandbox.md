# Nested incus containers fail to start inside a sandbox (forkstart exit 1)

## Problem

Spec 08 (the `--local` backend) changes `container/volumes.py` (kept work
volumes) and adds a recovery path that launches a helper container. The
project rules require a real-incus run for that kind of change
(`./scripts/smoke.sh`, `tests/integration`). A session tried to get one inside its
own sandbox (session gentle-germain, November 2026) and could not: incus
installs and initialises, but no nested container ever starts. So the local
backend's container side (bundle in over `incus exec` stdin, export,
kept-volume metadata via `user.*` keys, the read-only helper mount for
`--recover`) is **not verified against real incus**.

Without a working nested incus, no sandbox session can verify changes to
the container-facing modules, and they all fall back to "Not verified against
real incus".

## What was done, exactly

Inside the sandbox (Ubuntu, unprivileged container; `/proc/self/uid_map` is
`0 1000000 1000000000`; no `/dev/kvm`; `Seccomp: 2`, one filter). incus 6.0.6
was preinstalled from `claude_sandbox_deps.yaml`.

1. `sudo incus admin init --minimal` failed:
   `Failed to create local member network "incusbr0" ... Failed to check
   dnsmasq version: ... exec: "dnsmasq": executable file not found in $PATH`.
   Fixed with `sudo apt-get update && sudo apt-get install -y dnsmasq-base`
   (it isn't in the deps file).
2. Applied the launcher's own `NESTING_SETUP` steps by hand (subuid/subgid
   `root:100000:65536`, restart `incus.socket incus`), re-ran
   `incus admin init --minimal`; the first failed init had left the default
   profile without a root disk, fixed with
   `incus profile device add default root disk path=/ pool=default`.
   Storage pool `default` uses the `dir` driver (so no quotas either).
3. `incus launch images:ubuntu/24.04 probe-ct` downloads the image, then:
   `Error: Failed instance creation: Failed to run: /usr/libexec/incus/incusd
   forkstart probe-ct /var/lib/incus/containers /run/incus/probe-ct/lxc.conf:
   exit status 1`. `incus info --show-log probe-ct` printed only
   `Error: Invalid PID '<garbage>'`.

## Likely cause (not confirmed)

The outer sandbox was probably launched without `--nesting`
(`security.nesting=true`), which nested LXC needs (it mounts proc/sys and
uses nested user namespaces). The session could not check the outer
container's config from inside. `dnsmasq-base` missing is a second, smaller
gap.

## What "done" looks like

A session started with the flags meant for this (e.g. `./run.sh
studcavity/claude_sandbox --nesting`) can run `./scripts/smoke.sh` (or
`CS_INTEGRATION=1 python -m pytest tests/integration -m incus`) to
completion inside the sandbox, and the README/CLAUDE.md say which flags that
takes.

## Options

- **Relaunch with `--nesting` and retry** (first step: it may simply work).
  Record the result here or in the README.
- **Add `dnsmasq-base` to `claude_sandbox_deps.yaml`** so `incus admin init`
  works without a manual install (only useful together with nesting).
- **Make `NESTING_SETUP` also repair a half-initialised default profile**,
  or fail with a clear message, since a failed first `admin init` leaves no
  root disk device.
- **Accept that container changes are verified on the host only**, and say
  so in CLAUDE.md, so sessions stop trying.

- **Make `scripts/final-check.sh`'s incus probe launch something.** It decides
  whether to run `tests/integration` with `incus info`, which succeeds once
  incus is initialised even when no container can start (exactly the state
  above). Every integration test then fails with the `forkstart` error
  instead of being skipped. A probe that launches and deletes a tiny
  ephemeral container (or `self_check`s one) would tell the two apart.

I lean towards the first two together; the last is the fallback.

## Risk

`--nesting` widens what the sandbox can do to its host kernel (nested user
namespaces, more mounts). It is already an opt-in flag; keep it opt-in.

## Also outstanding from spec 08

Run `./scripts/smoke.sh` on a real host for the local backend: `./run.sh --local
<a scratch git repo>`, commit in the session, exit, and check that the branch
appears under `sbx/<session>/` with a review report; then kill a container
mid-session and check that `./run.sh --local <repo> --recover` imports its
work.

## Update (session brave-godel, November 2026)

Partly addressed on `sbx/brave-godel/01-nesting-installs-its-packages`:
`--nesting` now installs `incus` and `dnsmasq-base` itself (merged into the
deps, so baked into the cached deps image, even under `--no-deps`), and
`NESTING_SETUP` fails loudly when incus is missing instead of skipping. A new
smoke test, `test_nesting_installs_and_initialises_incus`, covers `--nesting`
through the real `--no-install-recommends` path.

Still open:
- Not verified against real incus: neither the new smoke test nor a nested
  container actually starting in a `--nesting` sandbox has been run. Run
  `./scripts/smoke.sh` on a host with incus. `init --minimal` may need more
  packages than `dnsmasq-base` (e.g. nftables); add them to `NESTING_APT`.
- `NESTING_SETUP` does not repair a half-initialised default profile.
- `scripts/final-check.sh`'s incus probe still uses `incus info`.
- The spec 08 local-backend smoke run above.
