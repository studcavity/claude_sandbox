---
type: Reference
title: "The session's CLAUDE.md"
description: "How the sandbox's ~/.claude/CLAUDE.md is rendered, why it is kept small, and where on-demand detail lives."
tags: [claude-md, session, config]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: render
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/render.py
    title: Renders the session CLAUDE.md
  - id: tpl
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/data/sandbox_CLAUDE.md
    title: The template
---

Each session gets `~/.claude/CLAUDE.md` in the container, rendered from
`claude_sandbox/data/sandbox_CLAUDE.md` (`--claude-md PATH` uses a different
one).[^render] It tells the session that the container and key are thrown away
on exit, which repo and branch are reachable, whether the key can push, whether
`sudo` works and which tools exist, and it tells Claude to keep history linear
and to commit and push working steps so nothing is lost. Git in the container is
set to `pull.rebase=true` and `merge.ff=only` to back this up.

# Why it is short, and where the rest went

Everything in the file is loaded into every session's context, so a test caps its
size. It holds only what applies on every turn: that the container is ephemeral, the
session's identity and branches, the trust boundary, git habits and the per-session
facts (access, installed tools, mounts, USB). Guidance for a particular situation
(editing the deps file, writing a hazardous script, handing work back, asking for
hardware) lives in an OKF bundle shipped into the container at
`~/.claude/docs/sandbox/`, one variant per backend. The CLAUDE.md states the session's backend
(`$backend`, chosen at launch) and that path, and lists when to open which page, so the session finds
detail when it needs it and its context stays small. See
[Session docs](../../dev/architecture/session-docs.md) for how the bundle is built.
If a change makes anything the template says stale, fix it in the same branch.

# Template mechanics

Placeholders are `string.Template` fields filled in by `render_claude_md`; write
`$$` for a literal `$`. Facts that change per session (container name, session id,
branch prefix, mounts, USB) belong here; text that does not belongs in the session
docs, written without placeholders. Text that differs per backend goes in
`<!-- if github -->` / `<!-- if local -->` ... `<!-- end -->` blocks.

# Same text whatever the repo's protection state

The rendered file is identical regardless of how well the repo is protected (a
test checks it): a list of a repo's weak points is a map for a prompt-injected
session. The one exception is required matrix-entry checks (`test (3.12)`),
which the session can fix, so then (and only then) it is told to ask you about
adding an aggregator job. See
[Protecting the default branch](../security/branch-protection.md).

[^render]: Renders the session CLAUDE.md
