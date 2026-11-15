# Improvement specs

These specs are drawn from `docs/plans/simplification.md` after checking it
against the code. Each one describes a problem, the options, what "done"
looks like and how to verify it. None of them prescribes an implementation.
Each spec is meant to be its own PR.

| Spec | Plan item | Summary |
|---|---|---|
| [01](01-one-merge-gate.md) | 1 | One merge gate for the commit checker |
| [02](02-todo-file-is-the-request.md) | 3 | The `todo/` file is the follow-up request |
| [03](03-file-issues-after-merge.md) | 2 | File issues only after merge, from one ledger |
| [04](04-narrow-secret-scan.md) | 4 | A narrower secret scan, aimed at what the sandbox holds |
| [05](05-stacked-prs.md) | 5 (stacking) | Stacked PRs get no CI: close the gap or stop stacking |
| [06](06-one-landing-pipeline.md) | new, + 5 | **Security bug** in the setup-script hold; one landing pipeline |

## Suggested order

1. **06, problem 1 first.** It is a bug, not a simplification.
2. 05. Its decision changes what 01 has to cover.
3. 01, then 02, then 03 (in the plan's order).
4. 04 at any point. It is independent, but 01-03 remove some of its call
   sites.
5. 06's remaining parts, whenever they cause the least churn.

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
  actually removes before promising a number.

## Not turned into a spec

Plan item 6 (a staging repo) is a design question, not a targeted change.
The plan already says it needs its own design doc before any code.
