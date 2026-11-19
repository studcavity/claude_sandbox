---
type: Reference
title: "Keeping documentation in the repo"
description: "How to write and update project documentation as OKF bundles, and how the README and docs/ indexes stay in step; read at each milestone and before writing docs."
tags: [docs, okf]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: claude-md
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/data/sandbox_CLAUDE.md
    title: Previously in the session CLAUDE.md
---

Keep documentation in the open project repo as decomposed Open Knowledge Format
(OKF) v0.3 bundles: small, single-topic Markdown files with YAML frontmatter. Use the
installed skills: `okf-generate` to create, update, migrate or validate docs;
`okf-consume` to read them (and report trust and freshness) before relying on them.
Do not hand-roll the format.

- Default to `docs/`, with nested subdirectories and an `index.md` at every level of
  specificity. If the project has readers of different kinds, one bundle per
  audience (`docs/<audience>/index.md` with its own `okf_version`) keeps each
  reader's context small.
- The top-level `README.md` is a short human front door: what the project is, the
  problem it solves, and an index to `docs/`. Detail goes in single-topic concepts
  under `docs/`, each listed in its directory's `index.md`; when you add or move one,
  update that index and the README's index.
- At every milestone (typically when you hand control back after one or more
  commits), use `okf-consume` to find the docs relevant to the work. Ask the user
  whether they are out of date or need more detail about what changed; apply the
  answer with `okf-generate`. If the workstream has no docs, create them.
- Keep them succinct, pragmatic and human-readable. Beyond technical specifics,
  record what code cannot say: motivation, context, trade-offs, reference sources.
