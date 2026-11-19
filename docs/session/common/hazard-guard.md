---
type: Procedure
title: "Gating hazardous scripts so they refuse to run outside the sandbox"
description: "The guard line to put at the top of any script that could damage a real machine; read before writing a script that uses sudo, deletes files, installs packages or touches credentials."
tags: [scripts, safety]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: shell
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/shell.py
    title: Creates /etc/claude-sandbox at boot
---

# Trigger

A script in the repo may be run on a real machine, where it can damage the system
or leak into dotfiles. Apply this to any script that uses `sudo` or root, installs
or removes packages, writes outside the repo (`~`, `/etc`, `/usr`, rc files,
`~/.ssh`), does `rm -rf` or other bulk deletes, changes global git, pip or npm
config, runs unverified downloads, writes to hardware, kills processes or touches
credentials. When in doubt, add it.

# Steps

1. Put this guard at the very top, before any side effect:

   ```bash
   # Refuse to run anywhere but inside a claude_sandbox container.
   [ -f /etc/claude-sandbox ] || { echo "refusing: not inside a claude_sandbox container" >&2; exit 1; }
   ```

2. The launcher creates `/etc/claude-sandbox` at boot.[^shell] Mention the guard in the
   script's header comment.
3. Never run such scripts on the host, and never weaken or remove an existing guard
   to make a script "work" elsewhere. If the user wants one to run on their
   machine, say what it would change there and let them add a narrower, explicit
   opt-in instead.

[^shell]: Creates /etc/claude-sandbox at boot
