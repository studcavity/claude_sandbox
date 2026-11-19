---
type: Plan
title: "Simplifying the trust and actions layer"
description: "Proposal to simplify the trust and actions layer; its items became specs 01-07."
tags: [plan]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
---

# Simplifying the trust and actions layer

Status: proposal (written 2026-11-15 by session tender-lamport, after
branches `sbx/tender-lamport/1..6`). Nothing here has been started. Each item
is meant to be its own small PR; the order below is a suggestion.

## Why

The layer that turns a session's work into PRs, statuses and issues is
about 2,000 lines of source (`sandbox_actions`, `sandbox_check`,
`sandbox_land`, `sandbox_record`, `sandbox_untrusted`, `sandbox_labels`,
`sandbox_templates`, `sandbox_protect`, `sandbox_ghclient`) plus about 2,800
lines of tests, and a large part of the 488-line `sandbox_CLAUDE.md` teaches
sessions its protocol. Most of the size is duplication rather than
necessity:

- two merge gates for the same checker,
- two issue-filing paths,
- two idempotency ledgers,
- the same request stored in two places,
- a home-grown secret scanner beside one GitHub provides.

Every duplicate is code, tests, and a rule an agent has to follow correctly.
None of the cuts below weakens security; several make it stronger. The
security fixes already made (branches 1-6) are assumed merged.

## 1. Keep one merge gate for the commit checker

**Now:** the same `sandbox_check.check_findings` runs (a) as the
`commit-metadata` GitHub Actions job and (b) as the `sandbox/check` commit
status, which the launcher posts with the user's `gh` login
(`post_status_run`, `fetch_pr`, `--override`, `override_comment`,
`sync_check_label`, `gate_required`, `post_session_statuses`, and
`rulesets/require-sandbox-check.json`). `sandbox_ghclient` (and the PyGithub
dependency) exists mainly for (b).

**Options:**
- *A. Keep only the Actions job* (lean towards this). It needs no host,
  runs on every push to the PR (catching commits added after landing), can be
  pinned to GitHub Actions (`integration_id` 15368, done in branch 3), and is
  already required by `require-commit-metadata.json`. You lose: gating repos
  that don't carry the workflow. Removes roughly 300 lines and their tests.
- *B. Keep only the status.* Works in any repo with no workflow, but it only
  runs when the launcher (or a human) runs it, and a status posted with an
  OAuth token can't be pinned to an app.
- Note: `sandbox/provenance` (branch 5) is also a host-posted status and
  must stay host-side (the public keys live on the host). If B is chosen,
  merge the two posting paths into one.

**Done when:** one context gates merges; `sandbox_ghclient` is either gone or
used by more than one caller; README/CLAUDE.md describe one gate.

## 2. File issues only after merge, from one ledger

**Now:** `Sandbox-Action` issues are filed (a) by the launcher at session end
from **unmerged, unreviewed** branches (`sandbox_actions.run`) and (b) by the
post-merge filer (`sandbox_check.py --file-issues`). Supporting (a) needs the
`resolve` directive, per-session caps counted from `session:` labels, session
attribution of ids, and two ledgers (a marker in the issue body and a marker
comment on the commit).

**Proposal:** file only after merge. A human has then reviewed the text that
gets published under the user's name, which is a better trust boundary.
Then:
- drop the session-end filing path, the per-session cap and its label
  counting;
- keep one ledger: the marker on the issue's last line (drop
  `done_ids`/`mark_done` commit comments);
- `resolve` becomes unnecessary if the request is a file (item 3): deleting
  the file before merge withdraws it.

The filer can be a small workflow on `push` to the default branch with
`issues: write` (no host needed), or stay a host command. **Risk:** follow-ups
from sessions whose PRs are never merged are never filed. That is arguably
correct (unmerged work is unreviewed), but say so in the docs.

## 3. One place per request

**Now:** an issue request is a JSON trailer (title, body, id with a
`<slug>-<YYYYMMDD>-<session>-<4 random>` grammar) **and** a
`todo/<id>.md` brief in the same commit, cross-checked by CI.

**Options:**
- *A. The file is the request* (lean towards this). Any `todo/*.md` added on
  the default branch becomes an issue; its first heading is the title, the
  path is the id (unique by construction, so no id grammar, no random
  suffix, no date), deletion withdraws it. Removes the trailer parser's
  envelope/versioning, `id_problem`, `lint_commit`'s action rules, the
  `resolve` directive and most of the protocol text in `sandbox_CLAUDE.md`.
- *B. Keep the trailer, drop the file requirement.* Smaller change, but the
  brief then lives in a one-line JSON string.
- Old commits with trailers: keep a read-only parser for a release or two,
  or accept that unfiled historical items are dropped.

## 4. Use GitHub's secret scanning instead of our scanner

**Now:** `sandbox_untrusted.find_secrets` (regexes plus an entropy rule) runs
in four places: commit messages and added lines (checker), PR bodies
(`sandbox_land.safe_line`), issue bodies and briefs (`sandbox_actions`), plus
an allow-marker mechanism.

**Proposal:** enable GitHub **push protection**, which blocks a known
credential at push time, before it is in history anywhere, including pushes
by deploy keys. Optionally add gitleaks or trufflehog in CI for repos
without it. Keep at most the cheap prefix patterns (`ghp_`, `sk-ant-`,
private-key headers) as a last check before publishing text under the
user's name, and drop the entropy rule and the allow marker. **Check
first:** push protection availability for private repos on the user's plan,
and that it covers deploy-key pushes.

## 5. Smaller cuts

- **Optional "What the commits say" PR section** (`sandbox_record`,
  `record_section`): nice, but every line is untrusted text needing
  sanitising and a secret check. Consider linking to the commits instead.
- **Stacked-PR base discovery** (`choose_base`, up to 10 sibling compares per
  branch): keep only if stacking is actually used; otherwise target the
  default branch always.
- **`sandbox_labels` / `sandbox_templates`**: fold into their single callers
  once items 1-3 remove the other callers.
- **`sandbox_CLAUDE.md`**: after items 2-4, cut the protocol sections; target
  well under half the current length. It is loaded into every session's
  context, so its length is a recurring cost.
- **`commit-metadata.yml` and `tests.yml`**: after item 1, check that every
  required check is still produced by exactly one job.

## 6. Bigger direction: a staging repo (security as much as simplicity)

Deploy keys aren't branch-scoped, and pushes alone can trigger workflows
with secrets. Branches 1-5 contain this (freeze before landing, PR head
pinning, signatures, optional rulesets), but a structural fix also deletes
code: sessions push only to a private staging repo (e.g. `owner/repo-sbx`,
Actions disabled) with their deploy key; at landing the host fetches the
exact signed commits it checked and pushes them to the real repo itself.
That would make obsolete: the privileged-file hold race handling, head
pinning, most of the optional rulesets, the `workflow`-scope concern, and the
cross-session tampering caveats. It costs one extra repo per target and a
host-side push. Worth a design doc of its own before any code.

## Suggested order

1. Item 4 (configuration plus deletions; independent of the rest).
2. Item 1 (pick A or B).
3. Items 3 then 2 (the request format first, then the filing path that reads
   it).
4. Item 5 cleanups.
5. Item 6 design doc.

Each step: delete code together with its tests, keep `tests/test_lifecycle.py`
green, and say in the commit what is no longer enforced and why that's
acceptable.
