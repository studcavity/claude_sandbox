# 5. `sandbox_check.py`: from lint to merge gate and post-merge filer

**Decision:** the new verbs live on the **`sandbox_check.py` CLI**, not as new
launcher flags. (`--check` is what the program already does when given
`BASE HEAD`, so it stays implicit; the new behaviours are `--post-status` and
`--file-issues`.) The launcher gets at most a tiny call into the same
functions (5d); it adds no flags of its own beyond opt-outs.

## What exists
`python sandbox_check.py BASE HEAD [--branch REF]` runs
`sandbox_actions.lint_commit` over `BASE..HEAD` and prints `::error::`
annotations (exit 1 on any problem). The workflow
`.github/workflows/commit-metadata.yml` runs it on PRs, with the checker taken
from the PR's *base* so a branch can't weaken its own rules, read-only token,
no secrets, branch name passed through env. `rulesets/require-commit-metadata.json`
requires that job (`commit-metadata`) once applied. The launcher already
offers repo-defined `rulesets/*.json` through a strict allowlist
(`sandbox_protect.validate_repo_ruleset`). The generic `protect-main.json`
deliberately requires no check (it would block repos without it).

## Delta

### 5a. Make the checker stdlib-only
Required by plan 4d and by CI cost: remove the transitive `invoke` import
(`sandbox_sessions` -> `sandbox_github`). Test by importing with `invoke`
unavailable. `--post-status`/`--file-issues` import `sandbox_github` lazily,
only when asked.

### 5b. More rules, with severities
Add to `lint_commit`/a sibling `lint_range`, each `error` or `warn` (`warn` is
printed as `::warning::` and doesn't change the exit code; `--strict` promotes):
- **merge commits**: today `--no-merges` silently skips them, but linear
  history is a repo rule. Flag them (`error`).
- `fixup!`/`squash!` leftovers (`error`); conventional prefix (`warn`).
- `Verified:`/`Not verified:` required when a commit touches paths that need a
  real-environment run (default list: `sandbox_containers.py`,
  `sandbox_images.py`, `sandbox_deps.py`; `error`).
- `Resolves follow-up from <sha>` must name a commit in history (`error`).
- secret scan over added lines and messages using plan 3b's patterns (`error`).
- source changed with no test change (`warn`, heuristic).
Defaults live in code; a per-repo override file is a later, separate decision
(strict schema like `claude_sandbox_deps.yaml` if ever added).

### 5c. `--post-status OWNER/REPO`: gate merges with no workflow
- Runs the check, then posts a commit status on the head SHA:
  context **`sandbox/check`**, `success` or `failure` (description <= 140
  chars: "0 errors, 2 warnings"), `target_url` the PR if known. If the checker
  itself crashes, post `error` (red, distinguishable from violations), never
  `success`. Needs only the user's `gh` login (`repo:status`); no workflow.
- SHA discipline: resolve the branch tip from GitHub, scan *that* sha, and
  refuse to post if the tip moved meanwhile.
- By PR: `--post-status OWNER/REPO --pr N` resolves base/head itself (this is
  the break-glass re-run path, see the README).
- Gating is **opt-in per repo**: ship `rulesets/require-sandbox-check.json`
  (context `sandbox/check`, `strict`, same shape as the existing one) and
  document applying it exactly like `require-commit-metadata.json`. It must not
  go into the generic ruleset. Optional: the launcher offers it with the other
  rulesets (default no).
- Behaviours to expect (see the probe, below): a new commit loses the status
  until re-posted; stacked PRs each need their own; merge queues don't work with
  statuses; anyone with write access can post that context (statuses from users
  can't be pinned to a source). The un-skippable property comes from the logic
  and status living on the host, outside anything a PR can edit.
- **Verify first with `scripts/probe_commit_status.sh`** (real GitHub,
  throwaway repo). Extend it with the break-glass steps (disable the ruleset
  via `enforcement: disabled`, confirm the merge goes through, re-enable) since
  the README runbook assumes a partial `PUT` works.

### 5d. Launcher wiring (kept tiny)
In `Sandbox.land()`, after the PRs open and before the actions run, call
`sandbox_check.post_status_for_session(repo, session)` when the repo opted in
(the ruleset exists) and `--no-check-status` wasn't passed; failures only warn.
Stacked branches get one status each. A failing check also labels the PR
`check-failed` (plan 4b) and appears in the exit summary.

### 5e. `--file-issues OWNER/REPO [--since SHA] [--dry-run]`: the post-merge filer
Why it still matters: `sandbox_actions.run` scans commits on *unmerged* session
branches at exit. Commits that reach the default branch without that run
(launcher crashed, `--no-actions`, merged first, pushed by other tooling) are
never filed. This mode scans `SINCE..HEAD` of the default branch from `git log`,
parses them with the existing `parse_commits`, and runs the existing `execute`;
the ledger (plan 3d: the issue marker) makes it safe to run twice and alongside
the launcher. Always `--dry-run`-able. Run it by hand, or:
- **Optional self-contained workflow** rendered by the launcher and proposed
  through a PR (held for review: `.github/`), triggered by `push` to the default
  branch (its definition comes from the default branch, so only reviewed code
  runs), `workflow_dispatch`, and optional `schedule`. It checks out the repo,
  vendors/pins the script (hash header, drift warned at launch), and uses
  `permissions: contents: read, issues: write` only, with `GITHUB_TOKEN`: no
  custody. No `uses:` of this repo anywhere.
- Create-only: issues, and comments only on issues it created. Closing is a
  human act.

### 5f. Hostile input (unchanged rule, extended)
Everything read is agent-written text. Never interpolate event fields into
`run:`; keep `git` argv lists (as today); escape workflow-command syntax
(`::error::` injected through a commit message) in annotations; bound line
counts and sizes.

## Tests
Fixture repos in `tmp_path` (the existing `tests/test_check.py` pattern): each
new rule passing and failing; severities and `--strict`; merge-commit
detection; stdlib-only import; `--post-status` payload, `error` on crash,
refusal when the tip moved, PR resolution (faked `api`); `--file-issues` over a
range with dedupe on re-run and dry-run output; annotation escaping; rendered
workflow has no `uses:` of this repo, pins actions by SHA, requests only the
listed permissions.

## Not verified / needs a real run
`repo:status` with the user's login; ruleset behaviour for a never-reported
context and for `enforcement: disabled`; whether comments on commits need
`contents: write` for `GITHUB_TOKEN` (3d avoids depending on it). All are
covered by running `scripts/probe_commit_status.sh` (extend as above).
