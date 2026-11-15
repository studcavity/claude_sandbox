# Improvement specs

These specs are drawn from `docs/plans/simplification.md` after checking it
against the code. Each one describes a problem, the options, what "done"
looks like and how to verify it. All seven are implemented on one branch, one
commit per spec (07 in three); each spec's `Status:` line says which option was
taken. Spec 08 came later, on its own branch. None of them prescribes an implementation.
Each spec is meant to be its own PR.

| Spec | Plan item | Summary |
|---|---|---|
| [01](01-one-merge-gate.md) | 1 | One merge gate for the commit checker |
| [02](02-todo-file-is-the-request.md) | 3 | The `todo/` file is the follow-up request |
| [03](03-file-issues-after-merge.md) | 2 | File issues only after merge, from one ledger |
| [04](04-narrow-secret-scan.md) | 4 | A narrower secret scan, aimed at what the sandbox holds |
| [05](05-stacked-prs.md) | 5 (stacking) | Stacked PRs get no CI: close the gap or stop stacking |
| [06](06-one-landing-pipeline.md) | new, + 5 | Setup-script review hold skipped under `--land-all` / `--no-deps`; one landing pipeline |
| [07](07-untrusted-ci-context.md) | new | Check that CI runs of session code are untrusted, and offer the settings that make them so; repo security gaps (including branch protection) shown only as host-side warnings, never to the session |
| [08](08-local-backend.md) | new | A local-repo backend: work on a host git repo through bundles in and out, with a `Backend` refactor |

## Suggested order

1. 06, problem 1. It is a small inconsistency in a review safeguard (no
   unreviewed code runs because of it), but it is cheap to fix.
2. 05. Its decision changes what 01 has to cover.
3. 01, then 02, then 03 (in the plan's order).
4. 04 at any point. It is independent, but 01-03 remove some of its call
   sites.
5. 06's remaining parts, whenever they cause the least churn.
6. 07 before choosing option B in 05. Its first step, checking whether a
   deploy key can push workflow files, is worth doing early whatever else
   happens.

## Corrections to the plan, found during review

- `sandbox_ghclient` is also used by `sandbox_provenance`, so it is not
  "mainly for" `sandbox/check` (spec 01).
- No commit on `main` has ever carried a `Sandbox-Action` trailer, and `main`
  has no `todo/` files, so item 3 has nothing to migrate in this repo
  (spec 02).
- Both workflows run only for PRs whose base is `main`, so stacked PRs are
  never checked by CI (spec 05).
- **`sandbox_CLAUDE.md` length.** The plan targets "well under half" of its
  roughly 490 lines after items 2-4. But the protocol section those items
  remove ("Commit messages carry point-in-time state") is about 55 lines.
  Reaching that target would also mean cutting non-protocol sections
  (deps-file format, quality gates, git rules). Measure what each spec
  actually removes before promising a number. **Measured after implementing:**
  `sandbox_CLAUDE.md` went from 330 to 311 lines (20.2k to 18.8k chars), not to
  "well under half": the protocol section shrank, but the boundary rule and the
  state-independent protection text were added.

## Not turned into a spec

Plan item 6 (a staging repo) is a design question, not a targeted change.
The plan already says it needs its own design doc before any code.
