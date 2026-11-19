---
okf_version: "0.3"
title: "claude_sandbox for operators"
description: "How to run, configure and secure claude_sandbox, the launcher that runs Claude Code sessions in throwaway Incus containers; read it before launching sessions or setting up a repo for them."
---

# claude_sandbox for operators

For the person at the host terminal who launches sandboxes. The introduction is
the repository's [README](../../README.md). What the session inside the
container is told is in the session bundle ([github](../session/github/index.md) or [local](../session/local/index.md) variant, per launch); how the
launcher is built is in the [development bundle](../dev/index.md).

* [Guides](guides/index.md) - Launching, local repos, concurrent sessions and PRs, images, host access.
* [Reference](reference/index.md) - The deps file, the final check, the session's CLAUDE.md.
* [Security](security/index.md) - Threat model, branch protection, provenance, Actions hardening, secrets.
