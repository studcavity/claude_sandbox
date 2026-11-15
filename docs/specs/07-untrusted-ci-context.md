# Spec 07: make "nothing trusted runs before review" checkable, and offer to apply it

Source: a review discussion of specs 05 and 06, not
`docs/plans/simplification.md`. Status: open.

## The principle

**Nothing runs in a trusted execution context before a human has reviewed
it.** Session-written code running in CI is fine, as long as that run is
*untrusted*. In practice that means:

- it has no write access to the repo, or to anything that gates a merge
  (statuses, reviews, rulesets);
- it can reach no secrets;
- it is auditable: it runs on GitHub's runner and its logs stay there.

Today `tests.yml` runs session code on every same-repo PR (and the plan's
option B in spec 05 would add more runs). That is fine only if the repo's
settings make those runs untrusted. Those settings are per repo, the
launcher works with any repo, and right now README only tells the user to
set one of them by hand ("Settings -> Actions -> Workflow permissions to
read-only", in the provenance section).

## What GitHub offers (checked against the REST docs)

- **No setting requires approval for same-repo PR runs.** Every approval
  setting covers fork PRs only:
  `PUT /repos/{o}/{r}/actions/permissions/fork-pr-contributor-approval`
  (`approval_policy`) and, for private repos,
  `.../actions/permissions/fork-pr-workflows-private-repos`
  (`require_approval_for_fork_pr_workflows`, `send_secrets_and_variables`,
  `send_write_tokens_to_workflows`). Sandbox PRs come from same-repo
  branches and are opened with the user's own login, so none of these
  apply. A label-gated workflow is possible but lives in each repo's YAML,
  not in a setting.
- **What can be set deterministically with the API:**
  - `PUT /repos/{o}/{r}/actions/permissions/workflow` with
    `default_workflow_permissions: "read"` and
    `can_approve_pull_request_reviews: false`. This returns 409 when an
    owning org forbids the change.
  - `PUT /repos/{o}/{r}/actions/permissions` (`allowed_actions`,
    `sha_pinning_required`), if wanted.
  - Environments: `PUT /repos/{o}/{r}/environments/{name}` with a
    deployment branch policy (protected branches only) and required
    reviewers. This is the closest thing to "require approval" GitHub has
    for same-repo code: a job that names the environment waits for a
    person to approve it, and only that job gets the environment's
    secrets. The repo's workflow YAML has to opt in, so a setting alone
    can't do it. Required reviewers on private repos depend on the plan;
    check.
- **What can only be detected, not fixed:** repo-level and org-level
  Actions secrets (`GET /repos/{o}/{r}/actions/secrets`, and
  `.../actions/organization-secrets`), which return names only. The
  launcher can say "these N secrets are readable by any workflow that runs
  on a branch a session can push to". It can't move them, because their
  values are never readable.

## The gap the settings don't close (verify first)

`default_workflow_permissions` is only a *default*. A workflow that declares
`permissions:` in its own YAML gets what it declares. A secret becomes
reachable as soon as a workflow references it. So the question that decides
how far settings can go is:

> **Can a deploy-key push add or change `.github/workflows/*`?** If it can,
> a session can push a branch with a new `on: push` workflow. That workflow
> runs immediately, before any PR or hold, with any permissions it declares
> and any repo-level secret it names.

`sandbox_CLAUDE.md` says GitHub "normally rejects" such pushes, but the
`workflow` scope check applies to OAuth and App tokens, and it is not clear
that it applies to SSH deploy keys. Plan item 6 assumes such pushes work.
Check this on a scratch repo (with a no-op workflow, and not in a repo that
has secrets) before relying on either assumption. If they do work, the
remaining levers are:

- a **push ruleset** with a `file_path_restriction` on `.github/workflows/**`
  for non-admins. GitHub documents push rulesets for private and internal
  repos on Team and Enterprise plans only, so this needs checking per plan;
- **keeping secrets out of the repo level**: environment secrets with a
  protected-branch deployment policy aren't reachable from an `sbx/` branch
  at all, whatever its workflow declares;
- plan item 6's **staging repo**, where sessions never push to the real
  repo.

## Proposal

Add an "Actions hardening" check to the launcher, built the same way as
`Sandbox.protect_default_branch` / `offer_protection` and
`sandbox_protect`:

1. **Read** the current settings (the GETs above), plus which secrets
   exist, and turn them into a small verdict: which conditions of the
   principle hold, which don't, and which can't be read (a 403 without
   admin; a 409 from an org policy).
2. **On a terminal, offer** the settable changes (read-only default token;
   Actions may not approve PRs), defaulting to no. Show exactly what will
   change, and **apply** each with one PUT only after a yes. Never apply
   without a prompt; a `--no-…-prompt` flag skips it, like
   `--no-protect-prompt`.
3. **Report** what can't be set (repo-level secrets, whether push rulesets
   are available, the workflow-file push question) in the session's
   `CLAUDE.md` and in the exit summary, the same way `protection_note`
   reports branch protection.

Settings could live as declarative JSON, as `rulesets/*.json` do, so a
repo can see and pin what gets applied. Or they could stay in code, since
there are only two fields. Whoever implements this decides; the
`validate_repo_ruleset`-style allowlist applies if repos can supply their
own.

## Done when

- At launch, the launcher states whether the target repo meets the
  principle, and for each condition: true, false, or unknown and why.
- The settable conditions can be fixed with one "yes" and the matching API
  calls, after which a re-run of the check reports them as met (idempotent).
- Nothing changes without an explicit yes at a terminal. Org-policy
  refusals (409) and missing admin rights (403) are reported, never fatal.
- README replaces "set it by hand" with what the launcher offers. It
  documents the fork-only scope of GitHub's approval settings and the
  workflow-file push question, with the result of the check above.
- Spec 05's option B points here as satisfied, or says what is still
  missing.

## Verify

- Unit tests (pure): settings plus a secrets list → verdict. A diff of
  current against desired → the exact PUT bodies. 403 and 409 handling.
  `tests/test_lifecycle.py` extended for the new launch step.
- `tests/integration/test_gh.py` style: the real `gh` against the local
  fake server, for the new endpoints.
- On a scratch repo (needs a host): apply, then confirm with the GETs and
  in Settings; re-run and see nothing offered. Run the workflow-file push
  experiment above and record the result in the commit message.
