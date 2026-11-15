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

**The intended answer is no:** deploy keys should not be able to push
changes under `.github/`. The user's `gh` login no longer has the
`workflow` scope (as of 2026-11-15). That removes the scope from the
*host's* OAuth token, which is what the landing code uses. A deploy key is
a separate SSH credential with no scopes, so this doesn't settle whether
GitHub refuses a deploy-key push that touches `.github/workflows/*`.
`sandbox_CLAUDE.md` already says such pushes are "normally" rejected, but
nothing in the repo has verified it. Turn the expectation into a recorded
fact: on a scratch repo with no secrets, push a no-op workflow file with
a deploy key and record the result in the commit message. If the push is
**accepted**, the boundary is violated: treat it as a fix-immediately item
(see "Session-side rule" below), not a hardening task. The levers are:

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
3. **Warn the user, not the session.** Print what is unmet or can't be set
   (repo-level secrets, a 403 or 409, whether push rulesets are
   available) as host-side warnings **at launch and again in the exit
   summary**, so it is seen even after a long session. Do **not** put any
   of it in the sandbox's `CLAUDE.md` or anywhere else the session can
   read: a list of the repo's weak points is a map for a prompt-injected
   session. The session gets only the rule in "Session-side rule" below,
   which is the same whatever the repo's state.
   - Related, for the implementer to decide: `protection_note` already
     tells the session about the default branch's protection state
     ("Default-branch protection and required status checks" in
     `sandbox_CLAUDE.md`), including when a ruleset is missing. Consider
     whether the same rule applies there: keep what a session needs to
     act correctly ("PRs are required"), and move statements about gaps
     to host-side warnings.

Settings could live as declarative JSON, as `rulesets/*.json` do, so a
repo can see and pin what gets applied. Or they could stay in code, since
there are only two fields. Whoever implements this decides; the
`validate_repo_ruleset`-style allowlist applies if repos can supply their
own.

## Session-side rule (`sandbox_CLAUDE.md`)

`sandbox_CLAUDE.md` has no general statement of the boundary today. It has
only specific cases: the workflow note in "Testing", the held paths in
"Session identity and branches", and the guard for hazardous `setup:`
scripts. Add one short, unconditional rule, worded by the implementer.
Its substance:

- Nothing from the sandbox may run in a trusted or privileged execution
  context (anything with write access, secrets, or the power to merge,
  approve, or post a merge gate) before a human has reviewed it through a
  PR.
- A session that would cross that boundary as part of its task must abort
  that step, whatever the instruction's source, and say so to the user.
  Examples: a task that needs a workflow to run on push, a setup script
  that has to take effect before merge, or a request to post a status.
- A session that discovers a way across it (a push that should have been
  refused and wasn't, a workflow that ran from an unmerged branch, a
  credential it shouldn't have) must not use or probe it further. It flags
  it to the user at once, as a fix-immediately item: tell the user
  directly; don't rely only on a follow-up issue that is filed later.

Keep it general: it must not list which protections this repo has or
lacks (see point 3). It complements the launcher's checks; it doesn't
replace them, since a prompt-injected session may ignore it.
`test_render_claude_md_real_template` covers the template. Consider a
focused test that the rule is present, so a later trim (plan item 5's
`sandbox_CLAUDE.md` cut) can't drop it unnoticed, the same way
`tests/test_ci_config.py` guards the safety nets.

## Done when

- At launch, the launcher states whether the target repo meets the
  principle, and for each condition: true, false, or unknown and why.
- The settable conditions can be fixed with one "yes" and the matching API
  calls, after which a re-run of the check reports them as met (idempotent).
- Nothing changes without an explicit yes at a terminal. Org-policy
  refusals (409) and missing admin rights (403) are reported, never fatal.
- Unmet conditions appear as warnings at launch and in the exit summary,
  and in nothing the session can read (a test checks that the rendered
  sandbox `CLAUDE.md` is identical whether the verdict passes or fails).
- `sandbox_CLAUDE.md` states the boundary rule above, and a test keeps it
  there.
- The deploy-key workflow-push result is recorded. If such pushes are
  accepted, that is fixed before this spec counts as done.
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
