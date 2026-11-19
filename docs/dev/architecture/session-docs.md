---
type: Architecture
title: "Session docs: the bundle embedded in every container"
description: "How docs/session is assembled per backend and shipped into the sandbox, and why the always-loaded CLAUDE.md stays small."
tags: [architecture, session, docs]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: mod
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/session_docs.py
    title: session_docs.py
  - id: tpl
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/data/sandbox_CLAUDE.md
    title: The session CLAUDE.md template
---

# What and why

A session inside a sandbox needs two kinds of guidance: rules that apply on every
turn (the container is ephemeral, push early, stay on your branches, the trust
boundary), and procedures for particular situations (editing the deps file, gating a
hazardous script, handing a branch back, asking for a USB device). Putting both in
`~/.claude/CLAUDE.md` made it about 28k characters, all in context every turn. Now
CLAUDE.md carries the first kind and the per-session facts, and the second kind is an
OKF bundle the session opens on demand.[^mod]

The session is the third reader of this repo's docs, next to the operator and the
maintainer. It gets its own bundle, not the operator's pages, because the launcher
deliberately keeps a repo's protection gaps out of the session (see
[Threat model](../../host/security/threat-model.md)): shipping the security pages
would hand a prompt-injected session that map.

# Source layout

```
docs/session/
  common/     concepts both backends share
  github/     this backend's own concepts, and its root index.md
  local/      same, for --local
```

A launch copies `common/` plus the backend's directory, flattened, to
`~/.claude/docs/sandbox/` (one tar stream, like skills). The result is one OKF bundle
whose root `index.md` is the backend's. `common/` has no `index.md`, and a name may not
exist in both `common/` and a backend directory: the launcher refuses rather than guess
which wins. Links inside the source use the flat, assembled layout, so they resolve in
the container, not in the source tree; tests assemble each bundle and check frontmatter,
index coverage and links.

Why assembled, not two full bundles: most concepts are identical for both backends and
copies drift. Why static files, not templates: per-session values stay in CLAUDE.md
(see [The session's CLAUDE.md](../../host/reference/sandbox-claude-md.md)), so the docs
need no placeholders and read the same in the repo and in the container.

# How the session finds the pages

CLAUDE.md says which backend *this session* uses (it is a per-launch choice, never a
property of the repo), where the bundle is, and lists the situation that
triggers each page. Those pointers are tested: every page CLAUDE.md names must exist in
the assembled bundle for that backend. The one safety invariant that must not depend on
the session choosing to read a page, the guard for hazardous scripts, is repeated in
CLAUDE.md as a single line.

# Adding or changing a page

Write it under `common/` if both backends share it, otherwise add a same-named page to
each backend directory. Add it to both `index.md` files and, if it needs a trigger, to
the "Sandbox docs" list in `sandbox_CLAUDE.md`. Use `okf-generate`.

[^mod]: session_docs.py
