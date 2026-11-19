---
type: Reference
title: "Containers inside this sandbox"
description: "Whether this sandbox can run nested containers (Incus), how to use them, and how to ask for the capability; read before a task that needs to start containers or VMs."
tags: [nesting, incus, containers]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T12:00:00Z" }
sources:
  - id: nesting
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/sandbox.py
    title: resolve_nesting, NESTING_SETUP and NESTING_NOTE
  - id: host
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/container/host.py
    title: Parses the nesting request in claude_sandbox_host.yaml
---

This sandbox is itself an Incus container. It can run containers of its own only
if the host user allowed it for this launch. Check `~/.claude/CLAUDE.md`
("Dependencies"): it says "Nested containers are ENABLED" or that the repo's
request was not granted. To test directly: `incus info >/dev/null 2>&1 && echo yes`.[^n]

# When it is enabled

`incus` is installed and initialised (`incus admin init --minimal`: a `dir` storage
pool and the `incusbr0` bridge), and its socket is world-accessible inside the sandbox.

```sh
incus launch images:ubuntu/24.04 t1     # needs network access to the image server
incus exec t1 -- bash -lc 'uname -a'
incus delete -f t1
```

- Nested containers share this sandbox's disk (a 20GiB volume) and memory; delete
  what you create and do not leave large images behind.
- Their files are not in the repo and not pushed; only commits are saved.
- Run only what the task needs. A nested container is isolated from this one, not
  from the kernel, and this sandbox's own confinement is looser than usual because
  nesting is on (see below).

# When it is not enabled

You cannot turn it on from inside: it is an instance setting the host applies at
launch. If the task needs containers, say so, and add a request to
`claude_sandbox_host.yaml` on the default branch (separate from the deps file; it
changes no image):

```yaml
nesting:
  description: Integration tests start Incus containers   # plain text, <=200; shown to the user
```

(`nesting: true` also works.) Commit, push, and tell the user to relaunch with
`--allow-nesting` (they are prompted otherwise on a terminal), or with `--nesting`
as their own grant. A request alone changes nothing.

# Why the host user decides

`security.nesting=true` loosens the container's confinement: mounts, user namespaces
and syscall filtering that normally block container managers are relaxed, so more of
the host kernel is reachable from code running here. The sandbox is still unprivileged
and gets no host sockets, mounts or devices, but a kernel or Incus bug is easier to
exploit. Do not request it for a task that does not need it, and mention in your final
message if you used it.

[^n]: Where nesting is decided and set up
