# Verify whether a deploy key can push `.github/workflows/*`

## Problem
The launcher's security model assumes a sandbox's deploy key **cannot** push a
change under `.github/workflows/`. If it can, a session can push a branch with a
new `on: push` workflow, which runs immediately, before any PR, hold or review,
with whatever `permissions:` it declares and any repo-level Actions secret it
names. That would break the rule "nothing runs in a trusted execution context
before a human has reviewed it" (see `docs/specs/07-untrusted-ci-context.md` and
the README section "Actions hardening").

Nothing in this repo has ever verified the assumption. Removing the `workflow`
scope from the host's `gh` token only constrains the *host's* OAuth token. A
deploy key is a separate SSH credential with no scopes, so it says nothing about
what GitHub does when a deploy key pushes a workflow file.
`sandbox_CLAUDE.md` currently says such pushes are "normally" rejected: that is
an expectation, not a recorded fact.

## Why it was not done
It needs a real GitHub repo, a deploy key with write access, and a host. The
sandbox that wrote this has a deploy key scoped to `studcavity/claude_sandbox`
only, and pushing a workflow file there would run it for real.

## What was ruled out
- Reading GitHub's docs: they describe the `workflow` scope for tokens and say
  nothing about deploy keys.
- Testing from the sandbox against this repo: unsafe (see above).

## How to check
On a host, with a **scratch repo that has no secrets** (Actions enabled):
1. Add a deploy key with write access (`ssh-keygen -t ed25519`, then
   `gh api repos/OWNER/SCRATCH/keys -f title=probe -f key="$(cat key.pub)" -F read_only=false`).
2. Clone over SSH with that key (`GIT_SSH_COMMAND='ssh -i key -o IdentitiesOnly=yes'`),
   add a harmless `.github/workflows/probe.yml` (`on: push`, one `echo` step),
   commit and `git push origin HEAD:refs/heads/probe`.
3. Record the exact result: accepted, or the rejection text (GitHub typically
   says the push was refused because of the `workflow` scope or missing
   `workflows` permission). Check the Actions tab: did the workflow run?
4. Delete the scratch repo (or at least the key and the branch).

## Done when
- The README section "Actions hardening" states the result (replace "Open
  question, not verified") and `sandbox_CLAUDE.md`'s "normally" wording is
  corrected if the result differs.
- If the push was ACCEPTED: treat it as fix-immediately, not hardening.

## Options if the push is accepted
- A **push ruleset** with a `file_path_restriction` on `.github/workflows/**`
  for non-admins (deploy keys are never admins). GitHub documents push rulesets
  for private and internal repos on Team and Enterprise plans only, so check the
  plan.
- **Keep secrets out of the repo level**: environment secrets with a
  protected-branch deployment policy are unreachable from an `sbx/` branch
  whatever its workflow declares.
- A **staging repo** (see `docs/plans/simplification.md` item 6): sessions never
  push to the real repo. I lean towards the first two as the cheaper ones; the
  staging repo is a design project of its own.
