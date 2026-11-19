---
type: Decision
title: "Spec 03: file follow-up issues only after merge, from one ledger"
description: "File follow-up issues only after the todo/ file reaches the default branch, from one ledger."
tags: [decision, spec]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
---

# Spec 03: file follow-up issues only after merge, from one ledger

Source: `docs/plans/simplification.md` item 2. Status: implemented, option A (workflow on push to the default branch; the same `sandbox_issues.py` runs by hand). It depends on
spec 02 (the request format) and is easier after it.

## Problem

Follow-up issues are filed by two paths:

1. **At session end, from unmerged branches.** `sandbox_actions.run` is called
   from `Sandbox.land` and from the `--land-all` branch of
   `claude_sandbox.main`. It compares every `sbx/<session>/*` branch with the
   default branch (`session_commits`) and files issues under the user's `gh`
   login from text no human has reviewed yet.
2. **After merge.** `sandbox_check.py --file-issues` (`file_issues_run`,
   `read_commits`, `DryRunApi`) scans the last N commits of a checkout.

Supporting path (1) takes:

- a per-session cap (`MAX_ISSUES_PER_SESSION`, `--max-issues`, counted from
  `session:` labels inside `known_issues`), and session attribution
  (`session_of`, `_ID_SESSION_RE`);
- `--max-actions` and `--no-actions` on the launcher and `action_limits`;
- **two idempotency ledgers**: the marker on the issue body's last line
  (`known_issues`, which scans at most `MAX_LEDGER_PAGES` × 100 issues), and a
  marker comment on the commit (`done_ids`, `mark_done`). Each has to heal
  the other.

There is also a placement problem. The post-merge filer lives in
`sandbox_check.py`, which must stay stdlib-only and is copied into every
sandbox as `sandbox-check`. So code that writes to GitHub ships into the
container. It is harmless there (no credentials), but it is in the wrong
module.

## Proposal

File only what has reached the default branch. A human has then reviewed the
text that gets published under the user's name, which is a better trust
boundary than "a session wrote it". Drop path (1) and everything listed above
that exists only for it.

## Options for who runs the filer

- **A. A workflow on `push` to the default branch** with
  `permissions: issues: write`. It needs no host and runs exactly once per
  push. The event's `before..after` range says what is new, so a ledger is
  needed only to make re-runs idempotent. Side effect: issues are created by
  `github-actions[bot]`, not under the user's name, which arguably helps
  rather than hurts. Cost: a workflow file (held for manual review by the
  launcher), and repos without the workflow get nothing.
- **B. The launcher, on the host**, scanning the default branch at session
  start or end since the last filed point. This works in any repo, but the
  timing is incidental: a merge is filed at the next launch.
- **C. Both**, as long as they share one ledger, so running both is safe.

Whichever you pick, move the filer out of `sandbox_check.py` into the module
that owns issue filing, so the checker's import graph (and
`SESSION_TOOL_MODULES`) stops including GitHub-writing code.

## Ledger

Keep one. The marker on the issue's last line is the natural one, because
it lives with the thing it records. Points to decide:

- The current lookup stops after 500 `sandbox`-labelled issues
  (`MAX_LEDGER_PAGES`). A search query on the marker, or a narrower label,
  avoids that limit. Option A's `before..after` range makes it matter less.
- With spec 02, the key is the `todo/` path (plus whatever spec 02 chose for
  reuse after deletion), not `(sha, id)`.

## Risk to document, not fix

Follow-ups from PRs that are never merged are never filed. That is arguably
correct (unmerged work is unreviewed), but sessions are currently told
"the launcher files these when the session ends", so update
`sandbox_CLAUDE.md` and the README to say "when merged".

## Done when

- One filing path and one ledger. The session-end filing call, its flags
  (`--no-actions`, `--max-actions`, `--max-issues`, unless you decide to keep
  a cap on the new path) and their tests are gone.
- `sandbox_check.py` no longer contains filing code. The checker's import
  graph and `SESSION_TOOL_MODULES` shrink to match.
- Running the filer twice on the same range files nothing new (test).
- `tests/test_lifecycle.py` stays green with the landing step no longer
  filing.
- The commit says what is no longer done: no issues from unmerged branches.

## Verify

- Unit tests: idempotency, a dry run, an oversized or secret-looking brief
  (refused), and a re-run after a partial failure.
- On a scratch repo (needs a host, or a workflow run): merge a PR that adds a
  `todo/` file, see exactly one issue, re-run, still one.
