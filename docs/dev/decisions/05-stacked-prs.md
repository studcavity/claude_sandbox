---
type: Decision
title: "Spec 05: stacked PRs: close the CI gap, or stop stacking"
description: "Stacked PRs get no CI: close the gap or stop stacking."
tags: [decision, spec]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
---

# Spec 05: stacked PRs: close the CI gap, or stop stacking

Source: `docs/dev/plans/simplification.md` item 5 ("Stacked-PR base discovery").
While checking it against the code I found a gap, which is why it is a spec
of its own. Status: implemented, option A (no stacking; stacking usage was not measured: no gh token in the sandbox).

## Problem

`sandbox_land.land_branch` targets each PR at the closest of the session's
other branches that it contains (`choose_base`). It runs up to
`MAX_SIBLINGS` (10) extra compare calls per branch to find it, plus one more
compare for the incremental diff. `format_landing` marks stacked PRs, and
`sandbox_CLAUDE.md` ("Session identity and branches") encourages sessions to
stack, with rebase instructions for after the parent merges.

**The gap: stacked PRs get no CI.** Both `.github/workflows/tests.yml` and
`.github/workflows/commit-metadata.yml` trigger on
`pull_request: branches: [main]`. That filter matches the PR's *base*, so a
PR whose base is `sbx/<session>/1-x` runs neither workflow. Then:

- When the parent merges and its branch is deleted, GitHub retargets the
  child PR to `main`. The retarget is a `pull_request` **`edited`** event.
  That type is not in the default activity types (`opened`, `synchronize`,
  `reopened`), so nothing runs until someone pushes again.
- If the repo doesn't delete merged branches, the child stays pointed at the
  stale parent branch and never gets CI.
- `sandbox_CLAUDE.md` already says this in passing ("PRs targeting a
  non-default base may not trigger CI"), but it puts the burden on the
  session, which can't see CI.

With required checks this fails closed: the check never reports, so the
merge is blocked. That is safe but confusing. If the required checks aren't
configured, a stacked PR can be merged into its parent with nothing checked,
and the parent then carries that code to `main`. The parent's own CI covers
only the parent's head at the time it last ran.

`sandbox/check` and `sandbox/provenance` *are* posted for stacked PRs,
because the launcher posts them for every PR it opens. If spec 01 picks
"Actions only", stacked PRs lose their only checker run.

## Is stacking used?

There is some evidence it is. The plan refers to `sbx/tender-lamport/1..6`,
and README shows a stacked example. But nothing measures it. Look at the
recent merged PRs' base branches (`gh pr list --state merged --json
baseRefName`, on a host) before choosing.

## Options

- **A. Stop stacking.** Always target the default branch. Delete
  `choose_base`, `MAX_SIBLINGS`, the sibling compares, the stacked marks in
  `format_landing`, and the stacking text in `sandbox_CLAUDE.md` and README.
  Sessions can still build branches on each other; each PR then shows the
  cumulative diff, so the reviewer sees more but every PR gets CI. This is
  the simplest option.
- **B. Keep stacking, extend CI to stacked PRs.** Trigger on PRs to any base
  (or `main` plus `sbx/**`), and add `edited` to the types so a retarget
  runs CI. **Security catch:** `commit-metadata.yml` takes the checker from
  `pull_request.base.sha`, so on a stacked PR the checker would come from
  the session's own unreviewed branch, and a branch could weaken the rules
  it is judged by. The checker must instead come from the default branch,
  for example by checking out `github.event.repository.default_branch`.
  Changes under `.github/` are held for manual review.

  **Caveat: more runs of unreviewed code.** On every same-repo PR,
  `tests.yml` already runs code the session wrote (pytest, `conftest.py`,
  anything they import) before a human has reviewed it. GitHub has no
  setting that requires approval for runs from same-repo branches: its
  approval settings apply only to fork PRs (spec 07). Option B adds more
  events and more PRs that do this. That is acceptable under the rule
  "nothing runs in a trusted execution context before review" only while
  those runs are untrusted: a read-only `GITHUB_TOKEN`, no secrets they can
  reach, and no way to post or approve anything that gates a merge. Spec 07
  is how a repo gets there and checks it. Choose B only together with 07,
  or after checking those conditions by hand for every repo that uses
  stacking. Option A does not add any runs.
- **C. Keep stacking in the launcher, but have it run the checks.** The
  host posts statuses for stacked PRs only. This works only if spec 01
  keeps a host-posted gate.

My lean is A unless the measurement shows stacking is common. Whoever
implements this should decide.

## Done when

- Every PR the launcher opens gets the required checks, whatever its base,
  or the launcher no longer opens PRs that can't get them.
- If A: the code, tests and docs for stacking listed above are gone. The
  per-branch GitHub calls in `land_branch` drop to a fixed number.
- If B: a test in `tests/test_workflows.py` / `tests/test_ci_config.py`
  pins that the checker is taken from the default branch, not from the PR
  base.
- `tests/test_lifecycle.py` stays green.

## Verify

- Unit tests for `land_branch`'s new base selection (A), or for the
  workflow structure (B).
- On a scratch repo (needs a host): two branches, the second built on the
  first. Land them, merge the first, and see that the second gets its
  checks with no extra push.
