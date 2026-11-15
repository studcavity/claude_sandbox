# Implementation plans

Most of this is now **built and merged** (#7, on `main`; status below). Each file is self-contained so a fresh session can pick up what remains.
Status is tracked here; update it in the commit that changes it. "Built" means
implemented and unit/integration-tested here; almost nothing has run against a
real incus or a real GitHub with this code, and each row says what is unproven.

| # | Plan | Status | Not yet proven / not done |
|---|------|--------|---------------------------|
| 1a | `--timings` | **built** | no real-host numbers yet: run `--timings-json` |
| 1b | concurrent GitHub reads | **built** (`--no-prefetch` to disable) | saving unmeasured (~4 round trips expected) |
| 1c | stale-while-revalidate images | **built** (`--hard-max-age`, `--refresh-images`) | detached refresher never run on real incus: `./smoke.sh` |
| 1d | fewer container execs | **built** (~13 fewer) | per-exec cost unmeasured; needs `./smoke.sh` |
| 1e | storage-driver note | **built**, plus a real bug fix: the pool check never worked on real incus | other drivers' output shape assumed |
| 2 | typed client (PyGithub) | **partly built**: `sandbox_ghclient.py`, used by the `sandbox/check` path; spike showed a dict facade over PyGithub is unsafe | remaining callers still on `gh_api` (see 02) |
| 3a-3f | sanitizer, secret scan, labels, issue-as-ledger, briefs, caps | **built** | real GitHub |
| 3g | typed-client seam | **partly**: see plan 2 | |
| 4a-4d | PR record, PR labels, templates, `sandbox-check` in the sandbox | **built** | real container install; offering `.github` templates to repos: not done |
| 5a-5d | stdlib checker, rules + allowlist, `--post-status`/`--override`, launcher posts it at exit | **built** | posting to a real PR with this code; ruleset not applied anywhere yet |
| 5e | `--file-issues` | **built** | the optional rendered `main` workflow: not done |

Merged: `credit-awareness` (#6, a note in the sandbox CLAUDE.md that running
out of credits loses work) and the implementation (#7, everything in the table).
Still open: `work` (first-run dialogs, `sandbox_claude_config.py`), rebased onto
`main`, and this `plans` branch (docs and the probe script). There is no
`--low-credits` flag: it could only be as good as the user remembering to set it.
From PR #4 (`1979267`): `Sandbox-Action` / `Sandbox-Session` trailers,
`sandbox_actions.py`, `sandbox_check.py` plus the `commit-metadata` CI job, and
repo-defined rulesets from `rulesets/*.json`. Probe for commit-status gating:
`scripts/probe_commit_status.sh` (run on a host 2026-11-15: all 19 checks passed).

Plan files: [1 performance](01-startup-performance.md) |
[2 typed client](02-typed-github-client.md) |
[3 session actions](03-session-actions-delta.md) |
[4 content contract](04-issue-pr-contract-delta.md) |
[5 sandbox_check](05-sandbox-check-delta.md)

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

Next, in order: run `./smoke.sh` and `--timings-json` on a real host; try
`--post-status` / `--override` on a real PR; then apply
`rulesets/require-sandbox-check.json` to a repo (it is safe now that the
launcher posts the status; the root README's "don't apply yet" warning can go);
then move the remaining `gh_api` callers onto the typed client.

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
Use `disabled`, not `evaluate`: **verified 2026-11-15** (probe, `gh` 2.103.3,
owner `studcavity`, public repo) that `evaluate` is refused with HTTP 422 on
that account, so `disabled` is the only way to suspend the gate there. Also
verified in the same run: a *partial* PUT of `-f enforcement=disabled` keeps
the `required_status_checks` rule intact, merges then succeed, and a partial
PUT of `-f enforcement=active` closes the gate again (a fresh PR is blocked).
So the two commands above are correct as written; the full-file `--input`
fallback is only for a future change in GitHub's behaviour (the probe would
report it). Only the gate is suspended: the generic `protect-default-branch`
ruleset (PRs required, linear history) stays on.

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
