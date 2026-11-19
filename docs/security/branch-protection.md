---
type: Security Model
title: "Protecting the default branch"
description: "The rulesets that make PRs mandatory, the commit-metadata merge gate, required status checks, and the repo warnings shown to the host user."
tags: [security, github, rulesets]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: protect
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/github/protect.py
    title: Offers and applies rulesets
  - id: rulesets
    resource: https://github.com/studcavity/claude_sandbox/blob/main/rulesets/protect-main.json
    title: The default ruleset
---

A deploy key can push to *any* branch, so the `sbx/<id>/` prefix is advice, not a
permission. The enforcement is GitHub-side, in rulesets.[^protect]

# The default ruleset

```
gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/protect-main.json
```

It requires PRs for the default branch and blocks force-push and deletion. Review
it first, and note it also blocks *you* pushing straight to `main` (no bypass
actors). You need not run it by hand: when a launch finds the default branch does
not require PRs and you are at a terminal, it asks `Apply branch protections to
this repo? [y/N]` (default no), lists the effects, and applies the ruleset on a
yes. It then offers to also require the CI checks that **passed on the default
branch's latest commit**: names are repo-specific and a name that never reports
would block every merge, hence opt-in. It will not duplicate an existing ruleset
of that name, and a failure (no admin, a plan without rulesets) only warns.
`--no-protect-prompt` silences the offer; the warning still prints.

# Repo-defined rulesets

A repo can ship its own as `rulesets/*.json` on its *default branch* (reviewed and
merged, never a session's branch). If it has any, the launcher offers those
*instead of* the generic one, one yes/no each with the effects listed first, even
when the branch is already protected (GitHub layers rulesets). Because the file is
untrusted text that edits repo settings, it must pass a strict allowlist
(`validate_repo_ruleset`): default branch only, no bypass actors, enforcement
`active` or `evaluate`, and only the rule types `deletion`, `non_fast_forward`,
`pull_request`, `required_status_checks` and `required_signatures`. Invalid files
are skipped with a warning.

# Rulesets this repo ships

- `require-commit-metadata.json` requires the `commit-metadata` check (see
  `.github/workflows/commit-metadata.yml`), pinned to GitHub Actions
  (`integration_id` 15368) so no other app or token can satisfy it with a status
  of the same name. This is the **one merge gate for the commit checker**
  ([Spec 01](../decisions/01-one-merge-gate.md)). It re-runs on every push to the
  PR and takes its checker from the PR's base, so a branch cannot weaken the rules
  it is judged by. To get past a wrong failure use GitHub's own break-glass: an
  admin bypass, or temporarily setting the ruleset's `enforcement` to `disabled`
  (the audit log records it). A repo without the workflow is not gated.
- `require-sandbox-provenance.json`: see [Provenance](provenance.md).

There used to be a second gate, a host-posted `sandbox/check` status
(`require-sandbox-check.json`). It is gone. If you applied that ruleset every PR
is now blocked by a context that never reports: list rulesets with
`gh api repos/OWNER/REPO/rulesets`, note the id of `require-sandbox-check`, and
`gh api -X DELETE repos/OWNER/REPO/rulesets/<id>`.

A test keeps every required check name equal to a real CI job name, except
`sandbox/provenance`, which is a commit status.

# Required status checks

A CI job blocks a merge only if a ruleset lists it. To add the ones the launcher
named: fetch the ruleset (`gh api repos/OWNER/REPO/rulesets/<id> > ruleset.json`),
add this rule to its `rules`, and `gh api -X PUT repos/OWNER/REPO/rulesets/<id>
--input ruleset.json`:

```json
{"type": "required_status_checks", "parameters":
  {"strict_required_status_checks_policy": false,
   "required_status_checks": [{"context": "<check name>"}]}}
```

Use names that have passed; they must match the job's displayed name. The
launcher offers only static names, never matrix entries like `test (3.12)`, and
warns you (in the exit summary too) when CI has them. Renaming a required job
strands every open PR, so update the ruleset in step. For a matrix job require an
aggregator job instead (this repo's `tests-passed`: it `needs` the others, runs
`if: always()` and fails unless all succeeded), so matrix entries can change
freely.

# Warnings to you, not to the session

At launch and in the exit summary ("repo warnings") you are told when: the default
branch is not protected or that could not be verified; no status check is required
to merge; or CI jobs that passed on the default branch are not required. The
session is told only to use PRs, never push to the default branch, and tell you
before changing a CI job's name (see
[the session's CLAUDE.md](../reference/sandbox-claude-md.md)).

# Also

- Keep repo Actions secrets minimal. A PR from a same-repo branch runs the
  branch's workflow code *with* secrets; put real secrets in an Environment
  restricted to the default branch.
- Not provided: GitHub's merge queue (unavailable on personal-account repos) and
  auto-merge. The shipped rulesets leave "require branches to be up to date" off
  (a PR passes on its own checks, not against the latest default branch); turn
  `strict_required_status_checks_policy` on if you want that, at the cost of
  concurrent sessions' PRs going stale one after another.
- A session can still force-push or add commits to *another* session's branch,
  which is why the diff, not the branch name, is what you review.

[^protect]: Offers and applies rulesets
