# 4. Issue and PR creation: templates, validated and consistent

Goal: every issue and PR a session causes has the same labels, structure and
safety checks. Transport is plan 3 (A: launcher acts at exit, optionally C:
live broker); this plan is the *content contract* and the agent-facing tools.

## Single source of truth
- `templates/issue.md`, `templates/pr.md` in this repo, with required sections:
  Summary, Why, How verified, Not verified, Follow-ups, Session
  (`<session id>`), and a credit/usage line when `--low-credits` was set.
- `sandbox_forms.py` (plan 3) renders from these and validates that required
  sections are present and non-empty. The **launcher** (exit-time `sandbox_land`
  and the issue filer) and the **in-container scripts** use the same code, so
  launcher-opened and session-requested PRs are identical.
- For repos the user owns, the launcher can offer to add self-contained
  `.github/pull_request_template.md` and `.github/ISSUE_TEMPLATE/` files
  (rendered by the launcher, no reference back to this repo) through a normal
  PR, held for review because it touches `.github/`. Anything the repo would
  need beyond that is stated as a requirement in the sandbox CLAUDE.md.

## Agent-facing tools (installed by the launcher, root-owned, stdlib-only)
- Under plan 3 **A** there is nothing to call: the session writes
  `Follow-up:` lines / `Issue:` trailers in commits. A tiny helper
  `sandbox-intent --lint` runs the validators locally so the session sees a
  rejection *before* it exits, when it can still fix the commit message.
- If plan 3 **C** is ever built, add `sandbox-issue` and `sandbox-pr`: validate locally
  (fast feedback), then send to the broker; `--dry-run` prints exactly what
  would be created. The repo is fixed by the launcher, never an argument.
- Both are installed with the launcher's other in-container files (written with
  `put_file`, no shell building), and named in the sandbox CLAUDE.md as the only
  supported way. Honest limit, repeated there: the session has `sudo`, so the
  helper gives consistency and early feedback; enforcement is the host-side
  validators (A/C) plus plan 5's CI check.

## Labels
`sandbox` (all), `session:<id>`, plus allowlisted kinds (`follow-up`,
`needs-human`, `blocked`). The launcher creates missing labels on first use
(needs `issues: write` via the user's login: test it on a fresh repo). Label
names/colours live in one table in `sandbox_forms.py`.

## Tests
- Template rendering and section validation (missing, empty, extra sections).
- Label allowlist, mention stripping, secret patterns and near-misses, bounds,
  closing-keyword handling, PR head/base rules, fingerprint stability.
- Golden test: the PR body from `sandbox_land` and from the in-container
  helper are byte-identical for the same input.
- Needs a real run: label auto-creation, template files landing in a repo.
