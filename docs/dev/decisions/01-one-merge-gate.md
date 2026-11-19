---
type: Decision
title: "Spec 01: one merge gate for the commit checker"
description: "One merge gate for the commit checker: drop the host-posted status, keep the Actions job."
tags: [decision, spec]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
---

# Spec 01: one merge gate for the commit checker

Source: `docs/dev/plans/simplification.md` item 1. Status: implemented, option A (only the `commit-metadata` Actions job gates). Repos that applied `require-sandbox-check.json` must delete it (README has the commands).

## Problem

The same checker (`sandbox_check.check_findings`) gates merges twice:

1. **`commit-metadata` Actions job** (`.github/workflows/commit-metadata.yml`).
   It runs the checker from the PR's *base* and the PR's commits as data. It is
   required by `rulesets/require-commit-metadata.json`, and that ruleset pins it
   to GitHub Actions (`integration_id` 15368).
2. **`sandbox/check` commit status**, posted from the host with the user's `gh`
   login. The code: `sandbox_check.post_status_run`, `pr_details`, `fetch_pr`,
   `override_description`, `override_comment`, `sync_check_label`,
   `gate_required`, `pr_numbers`, `post_session_statuses`,
   `format_status_outcomes`; the CLI flags `--post-status`, `--pr` and
   `--override`; the launcher's `--no-check-status` and `report_statuses`, called
   from both `Sandbox.land` and the `--land-all` branch of `main`;
   `sandbox_labels.CHECK_FAILED`; `rulesets/require-sandbox-check.json`; and
   about 35 tests in `tests/test_check_status.py`.

Each gate is code, tests, docs (README "rulesets" section, `CLAUDE.md`), and
one more thing a user has to understand to unblock a PR.

## Where the plan needs correcting

- **`sandbox_ghclient` doesn't go away with gate (2).**
  `sandbox_provenance.check_pr` / `post_pr` use `GhClient.pr`, `.login` and
  `.post_status`. The plan's "done when: `sandbox_ghclient` is either gone or
  used by more than one caller" is already true today. What would become dead
  is the methods only (2) uses: `comment`, `ensure_label`, `add_label`,
  `remove_label`, and their integration tests in
  `tests/integration/test_ghclient.py`.
- **Host-side status posting stays either way.** `sandbox/provenance` has to
  be posted from the host (the public keys live there). Option A below removes
  one *caller* of that machinery, not the machinery itself.
- **Option A has a coverage gap the plan doesn't mention.** Both workflows
  trigger on `pull_request: branches: [main]`, so a **stacked PR** (base
  `sbx/<session>/...`) gets no `commit-metadata` run at all until it targets
  `main`. See spec 05. `sandbox/check` is posted for every PR the launcher
  opens, stacked ones included. Before dropping it, check that the stacked
  case is either covered or deliberately accepted.

## Options

- **A. Keep only the Actions job** (the plan leans this way; so do I). Its
  advantages hold up when checked against the code: it re-runs on every push
  to the PR, it is pinned to an app, it needs no host, and its checker comes
  from the base so a branch can't weaken it. You lose gating in repos that
  don't carry the workflow, and the `--override` break-glass path. With A, the
  break-glass path becomes GitHub's own: an admin bypass, or temporarily
  disabling the ruleset, which GitHub's audit log records.
- **B. Keep only the status.** Works in repos with no workflow, but it runs
  only when the launcher (or a person) runs it, so commits pushed after
  landing are not checked unless someone re-runs it, and an OAuth-posted
  status can't be pinned to an app. If B is chosen, merge the `sandbox/check`
  and `sandbox/provenance` posting paths into one "post host statuses for these
  PRs" step.
- **C. Keep both, but make one of them advisory.** Only worth it if a real
  user runs repos without the workflow. Ask before choosing this one.

## Done when

- Exactly one required context gates the checker's result. README,
  `CLAUDE.md`, `sandbox_CLAUDE.md` and `rulesets/` describe that one gate and
  how to get past a wrong failure.
- Code, flags, labels, rulesets and tests that only served the removed gate
  are deleted together. `pyflakes` finds no orphaned helpers, and
  `sandbox_ghclient` keeps only the methods something still calls.
- `tests/test_lifecycle.py` stays green and still covers landing →
  provenance → actions.
- The commit says what is no longer enforced (e.g. "repos without the
  workflow are no longer gated by the checker") and why that is acceptable.
- If A: a repo that had already applied `require-sandbox-check.json` would
  have every PR blocked by a context that never reports again. The release
  notes / README say to delete that ruleset, with the `gh api` command to do
  it.

## Verify

- `python -m pytest`, `pyflakes`, `mypy` (see `CLAUDE.md`).
- `rg -n "sandbox/check|STATUS_CONTEXT|CHECK_FAILED|override"` comes back
  empty or deliberate.
- On a scratch repo (needs a host with `gh`): open a PR, see the single gate
  go red on a bad commit and green after a fix, and see the documented
  break-glass path work.

## Also fix while here (stale text found during review)

- The `sandbox_check.py` docstring points to `docs/dev/plans/README.md` and
  `scripts/probe_commit_status.sh`, and neither exists on `main`.
- The header comment in `.github/workflows/commit-metadata.yml` still
  mentions `Sandbox-Session`, whose rules were removed in `1aea691`.
  Changes under `.github/` are held for manual review by the launcher.
