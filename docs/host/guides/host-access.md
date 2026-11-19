---
type: Guide
title: "Mounts, USB and nested containers"
description: "Give a sandbox host directories, USB devices or nested Incus, each only with the host user's approval."
tags: [mounts, usb, nesting, host]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: host
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/container/host.py
    title: Parses claude_sandbox_host.yaml
  - id: incus
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/container/incus.py
    title: Incus wrapper
---

A sandbox gets no host files, hardware or nested containers by default. Each
capability below needs your explicit grant, and a repo can only *request* them.
`claude_sandbox_deps.yaml` says what to *install* (baked into cached images);
`claude_sandbox_host.yaml` says what to *borrow from the host* at launch, so
editing the host file never rebuilds an image.[^host]

# Bind mounts

`--mount HOST[:CONTAINER][:ro|rw]` (repeatable) bind-mounts a host directory.
CONTAINER defaults to `/mnt/<name of HOST>`. Mounts are **read-only unless you
add `:rw`**; a read-write mount lets the session change your host files directly,
with no git or PR in between.

A repo can request mounts in `claude_sandbox_host.yaml`:

```yaml
mounts:
  - source: ~/datasets/reference     # absolute or ~/ path on YOUR machine
    path: /mnt/reference             # optional; default /mnt/<name>
    mode: ro                         # optional; ro (default) or rw
    description: Reference data the tests read   # plain text, <=200
```

Each request is shown at startup with a `[y/N]` prompt (default no; read-write
ones are marked) unless you pass `--allow-mounts`; without a terminal and without
that flag they are not attached. `--mount` is your own grant and never
prompted. `--no-mounts` ignores both. A request whose source is missing or whose
path is unsafe is logged and skipped.

# Host files

The same file can ask to copy pinned files from your machine into the sandbox's home:

```yaml
files:
  - name: tool-config
    host_path: ~/dotfiles/tool.toml   # on YOUR machine
    dest: ~/.config/tool/config.toml  # under the sandbox user's home
    sha256: <64 hex>                  # required; the file must match exactly
    mode: "0600"                      # optional
    on_error: skip                    # optional: fail (default) or skip
```

Each is shown with a `[y/N]` prompt unless you pass `--allow-files`; with neither a
terminal nor the flag it is skipped, and `--no-files` ignores them. Approved files are
part of the cached deps image's key. Details:
[the deps file reference](../reference/repo-deps-file.md).

Mounts are attached after the deps install and before any credential exists, and
the session's CLAUDE.md lists each one plus any request you declined. Refused:
`/`, system directories, `/tmp`, the session's `~/.ssh`, `~/.claude`,
`~/.config`, the home directory itself, the work directory, and overlapping
mounts.

# USB passthrough

```yaml
# claude_sandbox_host.yaml
usb:
  - name: pico-probe
    vendor_id: "2e8a"        # 4 hex digits, quoted
    product_id: "000c"
    description: Raspberry Pi Pico debug probe (CMSIS-DAP)  # plain text, <=200
    tools: [pyocd]           # optional hints
```

The file is a request; the repo cannot grant itself a device. A device is
attached only if you approve it:

- `--usb VID:PID` (repeatable): explicit grant, no repo file needed;
- the interactive `[y/N]` prompt (default no; skipped without a TTY);
- `~/.config/claude_sandbox/usb_allow` (`--usb-allow PATH`): lines of `VID:PID`
  or `VID:PID owner/repo`, attached without a prompt. It must be owned by you
  and not group- or world-writable.

`--no-usb` ignores everything. Devices are attached after the deps install
(installers never run with hardware present) and before any credential exists.
Incus attaches *every* host device with that vid:pid, mode 0660 owned by
`ubuntu`. The host must not be using the device, and two sandboxes cannot share
one. The session's CLAUDE.md lists what the container actually sees (a sysfs
probe). Only pass devices you own and trust: USB firmware can attack its host,
and a debug probe can rewrite whatever it is wired to.

# Nested containers (`--nesting`, or a repo's `nesting:` request)

`--nesting` sets `security.nesting=true` so containers can run inside the
sandbox (for example this repo's `tests/integration`). The launcher also installs
`incus` and `dnsmasq-base` (even with `--no-deps`, and baked into the cached deps
image), gives root a small subuid/subgid range, makes the Incus socket
world-accessible (the sandbox is throwaway and `ubuntu` already has passwordless
sudo) and runs `incus admin init --minimal`.

A repo can ask for it in `claude_sandbox_host.yaml`:

```yaml
nesting:
  description: Integration tests start Incus containers   # plain text, <=200
```

(`nesting: true` also works.) Like a mount, a request does nothing until you approve
it: a `[y/N]` prompt (default no), or `--allow-nesting`; without a terminal and
without the flag it is not granted, and the session is told so. `--nesting` is your
own grant and is never prompted. The session's CLAUDE.md says whether nesting is on.

**Security implications for the host.** Nesting is the one setting here that weakens
the container itself rather than adding a resource. The container stays unprivileged
(its root maps to an unprivileged host uid range) and gets no host sockets, mounts or
devices, but AppArmor and seccomp are relaxed and user, mount and network namespaces
become available inside, so code in the sandbox can reach more kernel surface (mount
handling, user namespaces, syscall interception). A kernel or Incus escape bug is
therefore easier to exploit from a nested sandbox than from a plain one. Grant it only
for repos you trust to run code on this machine, keep the host kernel and Incus
updated, and prefer a disposable VM host for untrusted work.

[^host]

# Bind mounts

`--mount HOST[:CONTAINER][:ro|rw]` (repeatable) bind-mounts a host directory.
CONTAINER defaults to `/mnt/<name of HOST>`. Mounts are **read-only unless you
add `:rw`**; a read-write mount lets the session change your host files directly,
with no git or PR in between.

A repo can request mounts in `claude_sandbox_host.yaml`:

```yaml
mounts:
  - source: ~/datasets/reference     # absolute or ~/ path on YOUR machine
    path: /mnt/reference             # optional; default /mnt/<name>
    mode: ro                         # optional; ro (default) or rw
    description: Reference data the tests read   # plain text, <=200
```

Each request is shown at startup with a `[y/N]` prompt (default no; read-write
ones are marked) unless you pass `--allow-mounts`; without a terminal and without
that flag they are not attached. `--mount` is your own grant and never
prompted. `--no-mounts` ignores both. A request whose source is missing or whose
path is unsafe is logged and skipped.

# Host files

The same file can ask to copy pinned files from your machine into the sandbox's home:

```yaml
files:
  - name: tool-config
    host_path: ~/dotfiles/tool.toml   # on YOUR machine
    dest: ~/.config/tool/config.toml  # under the sandbox user's home
    sha256: <64 hex>                  # required; the file must match exactly
    mode: "0600"                      # optional
    on_error: skip                    # optional: fail (default) or skip
```

Each is shown with a `[y/N]` prompt unless you pass `--allow-files`; with neither a
terminal nor the flag it is skipped, and `--no-files` ignores them. Approved files are
part of the cached deps image's key. Details:
[the deps file reference](../reference/repo-deps-file.md).

Mounts are attached after the deps install and before any credential exists, and
the session's CLAUDE.md lists each one plus any request you declined. Refused:
`/`, system directories, `/tmp`, the session's `~/.ssh`, `~/.claude`,
`~/.config`, the home directory itself, the work directory, and overlapping
mounts.

# USB passthrough

```yaml
# claude_sandbox_host.yaml
usb:
  - name: pico-probe
    vendor_id: "2e8a"        # 4 hex digits, quoted
    product_id: "000c"
    description: Raspberry Pi Pico debug probe (CMSIS-DAP)  # plain text, <=200
    tools: [pyocd]           # optional hints
```

The file is a request; the repo cannot grant itself a device. A device is
attached only if you approve it:

- `--usb VID:PID` (repeatable): explicit grant, no repo file needed;
- the interactive `[y/N]` prompt (default no; skipped without a TTY);
- `~/.config/claude_sandbox/usb_allow` (`--usb-allow PATH`): lines of `VID:PID`
  or `VID:PID owner/repo`, attached without a prompt. It must be owned by you
  and not group- or world-writable.

`--no-usb` ignores everything. Devices are attached after the deps install
(installers never run with hardware present) and before any credential exists.
Incus attaches *every* host device with that vid:pid, mode 0660 owned by
`ubuntu`. The host must not be using the device, and two sandboxes cannot share
one. The session's CLAUDE.md lists what the container actually sees (a sysfs
probe). Only pass devices you own and trust: USB firmware can attack its host,
and a debug probe can rewrite whatever it is wired to.

# Nested containers (`--nesting`)

`--nesting` sets `security.nesting=true` so containers can run inside the
sandbox (for example this repo's `tests/integration`). The launcher also installs
`incus` and `dnsmasq-base` (even with `--no-deps`, and baked into the cached deps
image), gives root a small subuid/subgid range, makes the Incus socket
world-accessible (the sandbox is throwaway and `ubuntu` already has passwordless
sudo) and runs `incus admin init --minimal`. Nesting loosens the sandbox's
isolation from the host a little; use it only for repos that need it.

[^host]: Parses claude_sandbox_host.yaml
