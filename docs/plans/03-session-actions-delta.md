# 3. Session actions: what is left after PR #4

**Status: mostly built.** PR #4 (`1979267`) implemented the "intents in git,
launcher acts" design this plan used to propose (then called option A), with a
better carrier than we planned. This file now lists only the remaining delta.
The principle is unchanged: **no key material in the container, none new on the
host**; the launcher acts with the user's existing `gh` login.

## Already exists (do not redo)
| Capability | Where |
|---|---|
| Versioned one-line JSON trailer `Sandbox-Action: {"v":1,"type":...}`; unknown/malformed lines are no-ops | `sandbox_actions.py` (spec in the docstring) |
| `issue v1` and `resolve v1` handlers; registry `HANDLERS`/`DIRECTIVES` for new `(type, v)` | `sandbox_actions.py` |
| `Sandbox-Session` trailer on every commit; ids `<slug>-<date>-<session>-<4>`; a `todo/<id>.md` brief per issue | `lint_commit`, `sandbox_check.py`, template |
| Exit-time filing with the user's login, after PRs open; `--no-actions` opt-out; also under `--land-all` | `Sandbox.land`, `main` |
| Idempotency ledger = a commit comment carrying `<!-- claude-sandbox:action-done id=.. -->` | `done_ids`, `mark_done` |
| Bounds and sanitising: <= 25 actions/run, title 120, body 4000, control chars removed, `@` neutralised | `MAX_*`, `clean_block`, `clean_line` |
| Misattributed commits (trailer names another session) ignored | `session_commits` |
| CI lint on PRs from the base's copy of the checker | `.github/workflows/commit-metadata.yml` |

Rejected earlier and still rejected: GitHub App tokens (key custody, readable
by an agent with `sudo`, can't enforce format), `Follow-up:`-line scraping
(superseded by the JSON trailer), a new `sandbox_forms.py` (reuse what exists;
see 3a). The host-side broker stays **deferred** as a future option: build it
only if live feedback during a session becomes necessary, as a new transport
for the same handlers.

## Delta (in order)

### 3a. One sanitizer
`sandbox_land.clean_text` (one line, backticks -> quotes) and
`sandbox_actions.clean_block`/`clean_line` do overlapping jobs and will drift.
Extract a single stdlib-only `sandbox_untrusted.py` (`line`, `block`, secret
scan) and have both modules import it. Pure refactor first, tests moved with it.

### 3b. Secret scan before anything is posted
Nothing scans the text a session wants to publish. Add to the shared module:
refuse (turn into a no-op with a reason) a title/body/PR text matching
`ghp_`, `gho_`, `github_pat_`, `sk-ant-`, `AKIA...`, private-key headers, or a
long high-entropy string next to `token|secret|key|password`. Rejected items
show up in the exit summary as "rejected: <reason>" rather than vanishing
(today `Rejected` is silent by design: keep silent for malformed lines, but
surface policy rejections). The same scan feeds plan 5's diff check.

### 3c. Labels
Issues are created with no labels. Add `sandbox` and `session:<id>` (the id is
already validated), creating missing labels on first use. Needs `issues: write`
via the user's login; test on a fresh repo. Table of names/colours in one place.

### 3d. The issue is the ledger (closes a known gap)
Follow-up in `1979267`: a crash between creating the issue and writing the
commit comment duplicates the issue on re-run. Fix: before creating, look for
an existing issue whose body carries the marker
`<!-- claude-sandbox:action sha=<sha> id=<id> -->` (list issues with label
`sandbox`, state all; match the marker). Keep the commit comment as a courtesy
link, not the source of truth. Side benefit: filing then needs only
`issues: write`, which plan 5's optional workflow wants (commit comments may
need `contents: write`; unverified).

### 3e. Read `todo/<id>.md` into the issue body
`1979267` says the launcher doesn't read the brief yet. Fetch it at the action's
commit sha (contents API with `ref=<sha>`, so a later deletion doesn't matter),
bound its size, sanitise, and append it under the short `body`; also link the
file at that sha. If the file is missing, file the issue anyway and say so (CI
already prevents this on session branches). Decide first whether to embed or
only link: embedding is more useful, linking is safer against huge or odd
content; recommend embedding up to a cap plus the link.

### 3f. Policy knobs (small)
Per-run cap exists; add a per-session cap on issues (say 10) so a looping
session can't use all 25 on one repo, and a `--max-actions N` host override.

### 3g. Typed client seam
Everything goes through an injected `api(method, path, body)`; keep that seam
when plan 2 swaps in PyGithub (fakes in `tests/test_actions.py` depend on it).
Adapt at the module edge rather than rewriting handlers.

## Tests (per item)
Sanitizer parity (old inputs give the same outputs); each secret pattern and
near-miss; label creation + idempotent re-use; crash-window test (issue exists,
no commit comment -> no duplicate); todo fetch with missing/oversized/deleted
files; per-session cap.
