---
okf_version: "0.3"
title: "claude_sandbox session guide (github backend)"
description: "Situational guidance for a Claude Code session in a claude_sandbox container backed by a GitHub repo (deploy key, PRs opened by the launcher); consult it when a task touches the deps file, scripts, commits, PRs, CI, issues or host resources."
---

# Session guide (github backend)

Task-specific guidance for a Claude Code session inside a claude_sandbox
container. `~/.claude/CLAUDE.md` holds the rules that always apply; read a page
here when its situation comes up.

* [Editing the deps file](deps-file.md) - Schema and rules for the repo's deps file, per-launch setup: scripts and the final check: scripts; read before creating or editing any of them.
* [Gating hazardous scripts](hazard-guard.md) - The guard line to put at the top of any script that could damage a real machine; read before writing a script that uses sudo, deletes files, installs packages or touches credentials.
* [Commit messages](commit-messages.md) - The Verified, Not verified and Follow-up lines a commit message should hold; read before writing commit messages for work that was or was not tested.
* [Testing and quality gates](testing.md) - What tests and checks a session's work must come with; read before committing code or reporting that tests pass.
* [Handing your work back](handing-back.md) - What to do before you finish: PR synopsis in the last commit, sandbox-check, held branches, and what to list in your final message.
* [Asking for a GitHub issue](follow-up-requests.md) - How to leave a follow-up issue request by committing todo/<name>.md and what to put in it; read when you find work you cannot or should not do now.
* [CI and workflows](ci.md) - How CI runs for a session's PRs, how to watch it, and the rules for editing workflows; read before touching .github/ or reporting CI status.
* [Host directories and USB devices](host-access.md) - How to find out what host resources this sandbox has, and how to ask for a USB device or mount; read before a task that needs hardware or host files.
* [When the sandbox lacks something](missing-capabilities.md) - What to do instead of faking or giving up when a task needs a device, permission, host service, credential or tool this sandbox cannot provide.
* [Keeping documentation in the repo](documentation.md) - How to write and update project documentation as OKF bundles, and how the README and docs/ indexes stay in step; read at each milestone and before writing docs.
