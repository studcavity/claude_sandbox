---
type: Decision
title: "Spec 02: one place per request (the `todo/` file is the request)"
description: "The todo/ file itself is the follow-up request, replacing commit trailers."
tags: [decision, spec]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
---

# Spec 02: one place per request (the `todo/` file is the request)

Source: `docs/dev/plans/simplification.md` item 3. Status: implemented, option A. Settled: a request is the commit that ADDS the file (path + adding commit), edits are ignored, deleting closes nothing; see `sandbox_todo.py`. Do this before
spec 03, which reads the format this spec defines.

## Problem

A session that wants a follow-up issue filed currently writes it down twice,
in the same commit:

- a `Sandbox-Action:` trailer: one line of JSON with `v`, `type`, `id`,
  `title`, `body`, where the id grammar is
  `<slug>-<YYYYMMDD>-<session>-<4 random>`; and
- a brief at `todo/<id>.md`.

CI cross-checks the two (`sandbox_actions.lint_commit`, `id_problem`, and
`sandbox_check.branch_session` for the session part of the id). Supporting
the trailer takes: the envelope/versioning rules and `scan_message` in
`sandbox_actions.py`; the `resolve` directive (`DIRECTIVES`, `REF_RE`,
`_resolved_refs`, `plan`'s superseded list); `_ID_SESSION_RE` / `session_of`;
the "Issues the launcher will file" block in `sandbox_record` /
`sandbox_land.record_section`; and about 55 lines of `sandbox_CLAUDE.md`
("Commit messages carry point-in-time state"), which also tell every session
to generate random suffixes with a Python one-liner. Agents get this wrong
often enough that `sandbox-check` exists partly to catch it.

## Facts that make this cheaper than the plan assumes

- **Nothing has ever used the trailer on `main`.**
  `git log origin/main --grep='^Sandbox-Action:'` is empty, and `main` has no
  `todo/` directory. The plan's migration question ("keep a read-only parser
  for a release or two") has no data to protect in this repo. Other repos
  that use the launcher might have some, so say in the release note that
  unfiled trailers stop being read.
- `sandbox_check`'s module-level imports pull in `sandbox_actions`, which pulls
  in `sandbox_labels` and `sandbox_templates`. That is why
  `claude_sandbox.SESSION_TOOL_MODULES` copies six modules into every sandbox.
  If the checker no longer needs `sandbox_actions`, that list shrinks
  (a test pins it to the real import graph, so it will tell you).

## Options

- **A. The file is the request** (the plan leans this way; so do I). A
  `todo/<name>.md` that exists on the default branch is a request. Its first
  heading is the title, the body is the issue text, and the path is the
  identity. Deleting the file before merge withdraws the request, so
  `resolve` goes away.
- **B. Keep the trailer, drop the file requirement.** This is a smaller diff,
  but the whole brief has to fit into a one-line JSON string, which is the
  part agents find hardest.
- **C. Keep the trailer, generate the file.** Rejected here: it keeps both
  places and adds a generator.

## Design questions to settle (A)

These are for whoever implements A to answer, not settled here:

- **Is the path a lasting identity?** If `todo/fix-ci.md` is filed, later
  deleted, and later re-added for a different problem, a ledger keyed on the
  path alone would treat the new file as already filed. Options: key the
  ledger on path plus the commit that added the file; or treat a path that
  appears again after deletion as new; or forbid reuse in CI.
- **Edits after filing.** Should a later change to a filed brief update the
  issue, add a comment, or be ignored? Ignoring it is simplest, and the issue
  can link to the file at its commit.
- **Deletion after filing.** Should it close the issue, comment on it, or do
  nothing? Closing issues under the user's name from session-written commits
  is a new power; the current design deliberately avoids it ("left for a
  human to close").
- **Filename rules.** Require something like `[a-z0-9][a-z0-9-]*\.md` so the
  path is safe in URLs, labels and markers with no escaping. Two sessions
  picking the same name produce an add/add conflict when the second one
  rebases, which is the desired behaviour.
- **What CI still checks.** Probably: the file has a heading, it is under a
  size bound, and its name matches the rule above. No session or date rules.

## Done when

- A session asks for a follow-up by adding one file. `sandbox_CLAUDE.md`
  explains this in a few lines and drops the JSON, id-grammar and
  random-suffix instructions.
- The trailer parser, `id_problem`, `resolve`, `lint_commit`'s action rules,
  `session_of` and the record's "issues" block are deleted with their tests,
  or kept read-only if you decide other repos need it, and the commit says why.
- `SESSION_TOOL_MODULES` matches the checker's new, smaller import graph.
- README and `CLAUDE.md` describe the new format. The `todo/<id>.md` paragraph
  in `sandbox_CLAUDE.md` and its `Sandbox-Action` example are gone.
- `test_render_claude_md_real_template` and the lifecycle test still pass.

## Verify

- Unit tests for the new rules: a valid file, a missing heading, a bad name,
  an oversized file, and a path reused after deletion (whatever behaviour you
  chose).
- `rg -n "Sandbox-Action|resolve|id_problem"` comes back empty or deliberate.
- Lint a branch that adds a `todo/` file with the in-sandbox `sandbox-check`.

## Out of scope

Who files the issue and when: spec 03.
