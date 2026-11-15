# Final check runs the repo's tests on one branch only

## Problem
When Claude exits, `Sandbox.final_check` (`claude_sandbox/sandbox.py`) runs each
`check:` script once, in the working copy, on whichever branch is checked out
at that moment. A session that leaves several `sbx/<session>/*` branches (each
becomes its own PR, or its own import with `--local`) gets tests, lint and
type checks on one of them. The others are never tested by the launcher, not
before and not after a rebase prompted by `claude_sandbox/mergecheck.py`, so a
PR can open with failing tests on a branch nobody ran them on.

## What was considered
`mergecheck.py` already enumerates every session branch and judges each one
alone against its base (conflicts, behind). It deliberately stops there: it
needs only git, whereas tests need the repo's own script and take minutes.
Nothing has been tried for running the scripts per branch.

## Options
- **A. Run each `check:` script once per branch**, switching with `git switch`
  (or a throwaway `git worktree`) and restoring the original checkout after.
  Simple and faithful to what CI will run; cost is N times the runtime (the
  repo's own check can run the real-container tests). Worktrees avoid
  disturbing the session's checkout and the `.venv`, but a script that
  assumes the repo root may need care.
- **B. Only test branches that changed since the last pass**, remembering each
  tip's sha across resume loops. Cuts the cost on re-runs; adds state.
- **C. Leave as is** and say so in the final message (today's behaviour).
Leaning towards A with a worktree per branch and B as a later optimisation.

## Where
- `claude_sandbox/sandbox.py`: `final_check` (loop over `check:` entries) and
  `merge_findings`; reuse the branch list from `claude_sandbox/mergecheck.py`.
- `claude_sandbox/resume.py`: the resume prompt should say which branch each
  failure came from.
- Tests in `tests/test_claude_sandbox.py` (`checking` fixture) and
  `tests/test_lifecycle.py`.

## Done when
A session with two branches, one with a failing test, resumes with that
failure attributed to the right branch, and passes only when every branch
passes. Verify with a unit test of the loop, and `./scripts/smoke.sh` for the
real container path (not runnable inside a sandbox without `--nesting`).
