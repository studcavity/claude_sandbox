# Implementation plans

Written down, **not yet implemented**. Each file is self-contained so a fresh
session can pick one up. Status is tracked here; update it in the commit that
changes it.

| # | Plan | Status | Depends on |
|---|------|--------|-----------|
| 1 | [Startup performance and `--timings`](01-startup-performance.md) | planned | none (do `--timings` first: it measures everything else) |
| 2 | [Typed GitHub client](02-typed-github-client.md) | planned (PyGithub) | none; keep the `api(method, path, body)` seam |
| 3 | [Session actions: delta after PR #4](03-session-actions-delta.md) | mostly built; 3a-3g remain | none |
| 4 | [Issue/PR content contract: delta](04-issue-pr-contract-delta.md) | planned | 3a (shared sanitizer) |
| 5 | [`sandbox_check.py`: merge gate + post-merge filer](05-sandbox-check-delta.md) | planned | 3b secret scan, 5a stdlib-only |

Already built (not plans): first-run dialogs pre-answered
(`sandbox_claude_config.py`), `--low-credits` and the credits section of the
sandbox CLAUDE.md (branch `sbx/relaxed-fermat/credit-awareness`, not yet
merged), and from PR #4 (`1979267`): `Sandbox-Action` / `Sandbox-Session`
trailers, `sandbox_actions.py` (issues filed at exit), `sandbox_check.py` plus
the `commit-metadata` CI job, and repo-defined rulesets from `rulesets/*.json`.
Probe for commit-status gating: `scripts/probe_commit_status.sh`.

## Decisions (settled by the user)
- Client library: **PyGithub**, the most used (plan 2).
- New verbs live on the **`sandbox_check.py` CLI** (`--post-status`,
  `--file-issues`), not as launcher flags; the launcher only makes a tiny call
  into the same functions (plan 5).
- Broker (live channel): **deferred**, kept as a future option only (plan 3).
- GitHub App tokens: **considered, won't do** (plan 3).
- No cross-repo reusable workflows or actions (see below).
- Status gating is **opt-in per repo** through a ruleset; never in the generic
  `protect-main.json`.

Open: nothing needed from the user to start 3a/3b, 5a/5b or `--timings`.
Run `scripts/probe_commit_status.sh --yes` on a host before building 5c.

## Break glass: commit-status gating (`sandbox/check`)
Applies once a repo has opted in (the `require-sandbox-check` ruleset is
active). The gate fails *closed*: if the status is missing or red, the PR can't
merge. That is the point, so every way out must be deliberate, narrow, loud and
undone afterwards. There is intentionally **no standing bypass**: repo-defined
rulesets may not name bypass actors (`validate_repo_ruleset`), so even admins
are bound until they change the ruleset itself. Sessions never do any of this;
they tell the user which step applies (stated in the sandbox CLAUDE.md).

| Situation | Symptom | Use |
|---|---|---|
| Launcher died before posting; new commit pushed by hand; rebase or force-push | status "expected" or missing on the head SHA | **Level 0** |
| The check reports a false positive on this PR, or a rule is wrong for it | red `sandbox/check` | **Level 1** |
| The checker is broken for every PR, `gh`/statuses API unavailable, or the status was never going to be posted (repo not launched through the launcher) | everything red or pending | **Level 2** |

**Level 0: re-run (anyone with write access, no override).**
```sh
python sandbox_check.py --post-status OWNER/REPO --pr 42
```
Resolves base/head from the PR, re-checks, posts for the current head SHA. If
it goes green, nothing else is needed.

**Level 1: override one PR, one SHA (audited).**
```sh
python sandbox_check.py --post-status OWNER/REPO --pr 42 \
    --override "false positive: <why, >= 10 chars>"
```
Posts `success` for that head SHA only, with the description
`OVERRIDDEN by <your login>: <reason>`, and leaves a PR comment with the full
reason, the findings it overrode and your login. A new commit invalidates it
(new SHA), so the override can't ride along with unreviewed changes. If
`sandbox_check.py` itself is the problem, the equivalent by hand (same audit
trail is on you: write the reason):
```sh
sha=$(gh pr view 42 -R OWNER/REPO --json headRefOid --jq .headRefOid)
gh api -X POST repos/OWNER/REPO/statuses/$sha -f state=success \
  -f context=sandbox/check -f description="OVERRIDDEN by $USER: <reason>"
gh pr comment 42 -R OWNER/REPO --body "sandbox/check overridden: <reason>"
```
Caveat: anyone with write access can post that context, so this is only as
trustworthy as your collaborators (statuses from users can't be pinned to a
source; check `creator` in the probe's output).

**Level 2: suspend the ruleset (repo admin, time-boxed).**
```sh
id=$(gh api repos/OWNER/REPO/rulesets --jq '.[] | select(.name=="require-sandbox-check") | .id')
gh api -X PUT repos/OWNER/REPO/rulesets/$id -f enforcement=disabled   # open the gate
# ... merge what must merge ...
gh api -X PUT repos/OWNER/REPO/rulesets/$id -f enforcement=active     # close it again
```
Use `disabled`, not `evaluate` (evaluate mode may need an Enterprise plan;
unverified). Whether a partial `PUT` leaves the other fields intact is also
**unverified**: the probe script should be extended to test exactly this before
relying on it; if it replaces the ruleset, re-send the full file instead
(`gh api -X PUT repos/OWNER/REPO/rulesets/$id --input rulesets/require-sandbox-check.json`
with `enforcement` edited). Only the gate is suspended: the generic
`protect-default-branch` ruleset (PRs required, linear history) stays on.

**After any level:** file an issue saying what was overridden and why (a
`Sandbox-Action` trailer in the next commit does it), fix the root cause, and
for Level 2 confirm `enforcement` is `active` again
(`gh api repos/OWNER/REPO/rulesets/$id --jq .enforcement`). Never edit or
remove the `sandbox/check` status of someone else's SHA to hide a failure;
the override path exists so the reason is recorded.

**What posts `error` vs nothing.** If the checker crashes, `--post-status`
posts state `error` (red, "check crashed") rather than nothing or `success`.
If the process is killed outright nothing is posted and the PR waits: Level 0.

## Ground rules that apply to all of them
- **No remote-repo dependency on this repo.** Never `uses:` a reusable
  workflow or action from `studcavity/claude_sandbox` (cross-repo access
  settings, private repos). Where that pattern would be tempting, either do it
  as deterministic code in the launcher (host side), or state it as a
  requirement with a copy-paste snippet in the sandbox CLAUDE.md. Self-contained
  files rendered by the launcher and proposed through a PR are fine.
- Follow `CLAUDE.md`: pure decisions, I/O at the edges, tests with each change,
  `Verified:` / `Not verified:` / `Follow-up:` lines in commits.
- Changes to `sandbox_containers.py`, `sandbox_images.py`, `sandbox_deps.py`
  need a real-container run (`./smoke.sh`) on a host with incus.
- Other sessions edit `claude_sandbox.py` `main()` and `sandbox_CLAUDE.md`
  concurrently. Keep new logic in new modules and wire it with the smallest
  possible diff; rebase before pushing.
