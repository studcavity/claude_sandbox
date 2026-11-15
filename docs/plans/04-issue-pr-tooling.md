# 4. Issue and PR creation through durable scripts + templates

Goal: every issue and PR a session creates has the same labels, structure and
safety checks, and the agent doesn't hand-roll `gh`/API calls.

## Pieces
1. **Scripts shipped by the launcher** (in this repo under `sandbox_tools/`,
   copied into the container at launch to `/usr/local/bin`, root-owned 0755):
   `sandbox-issue` and `sandbox-pr`. Python, stdlib only (no deps in the
   container), tiny, with `--dry-run`. They are the only documented way to file
   either; the sandbox CLAUDE.md says so.
2. **Templates** under `templates/` in this repo: `issue.md`, `pr.md`, each with
   required sections (Summary, Motivation/Why, How verified, Not verified,
   Follow-ups, Session `relaxed-fermat`, credit/usage note when
   `--low-credits`). The scripts render the body from flags/stdin into the
   template and **reject** missing sections, so the format can't drift.
3. **Server-side enforcement in the target repo** (the only enforcement an
   in-container script can't bypass): a `.github/pull_request_template.md`,
   `.github/ISSUE_TEMPLATE/`, and a CI check that validates PR bodies and
   required labels, made a required status check via the ruleset (see "Quality
   gates" / required-checks notes already in `sandbox_CLAUDE.md`). The launcher
   can offer to add these to repos the user owns, through a normal PR (it
   already holds back `.github/` changes for review, so this stays reviewed).

## Script behaviour (security and consistency)
- Fixed target: the repo is baked in at launch (`SANDBOX_REPO`), never an
  argument. Refuse anything else.
- Labels: always `sandbox` and `session:<id>`; others only from an allowlist.
- Titles: length-bounded, single line, no `@mentions` of real users, no
  `Fixes #` that would auto-close issues unless explicitly allowed.
- Body: size cap; secret scan (token patterns such as `ghp_`, `github_pat_`,
  `sk-ant-`, private key headers, the session's own token) with refusal;
  escape/strip `@user` and `@org/team` so a session can't ping people.
- `sandbox-pr`: head must be a branch under `sbx/<session>/` that is pushed;
  base must be the default branch or another of the session's own branches
  (matches `sandbox_land`'s stacking rule); idempotent: if a PR for the head
  exists, update rather than duplicate; never touches workflow files itself
  (the hold on privileged files from `sandbox_land` applies: share that
  predicate, don't duplicate it).
- Output: print the URL only. Exit codes distinguish validation vs API errors.
- Transport: one function `call(method, path, body)`; token variant (plan 3)
  reads the 0600 token file on every call (it may have been refreshed); broker
  variant swaps just this function.

## Launcher changes
- Reuse the validators on the **host** too: `sandbox_land` (the exit-time PR
  opener) should render PR bodies through the same template code so
  launcher-opened and session-opened PRs are identical. Put the shared logic in
  one stdlib-only module (`sandbox_forms.py`) imported by both the launcher and
  copied alongside the scripts. Single source of truth for labels and format.
- Add a template section telling the session: file issues/PRs only with these
  scripts, what the labels mean, and that the exit-time PR opener will skip a
  branch that already has a PR from `sandbox-pr`.
- Copy scripts and templates in `Sandbox` after `provision_key`; they are data
  for `$` template substitution only via `string.Template`, no shell building.

## Honest limits
The agent has sudo in its own container, so it can edit or bypass the scripts
and call the API with the token directly. The scripts give consistency and
catch mistakes, **not** security against a malicious session. Real enforcement
is the repo-side CI check (3 above) or the broker variant in plan 3.

## Tests
- Pure: template rendering, section validation, label allowlist, mention
  stripping, secret scan (each pattern, near-misses), title/body bounds, branch
  prefix rules, idempotency decision.
- Script CLI via subprocess with a fake transport; `--dry-run` prints exactly
  what would be sent.
- Same fixtures reused to assert `sandbox_land` output matches the scripts'.
- Needs a real run to verify: label creation on a repo without the labels
  (create-if-missing needs `issues: write`; test it) and CI template check.
