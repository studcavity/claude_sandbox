# 5. `--check`: validate the *work record*, in CI and after merge

Two modes of one verb (names decided), built on the shared validators and scanner (plan 3/4):

1. **`--check`** (read-only): run in a PR's CI. Validates every commit and the
   PR as one body of work. Exit 0/1, GitHub annotations, a job summary.
2. **`--check --file-issues`**: turns unresolved follow-ups and findings into
   tracked issues. Run manually, or on push to `main`, from a tightly scoped
   workflow.

## Naming (decided)
`--check` (read-only) and `--file-issues` (the filing mode; always combined
with `--check`). Modes are flags, like `--land-all` and `--setup-token`; the
positional REPOSITORY rules out subcommands. We never offer `--fix`: pushed
history is never rewritten.

## Ground rule: no cross-repo workflow or action dependencies
Nothing in a remote repo may depend on `uses: studcavity/claude_sandbox/...`
(reusable workflows, composite actions), because that needs cross-repo access
settings we don't control and breaks for private repos. Wherever we'd reach
for that pattern, one of two things replaces it:
1. **Deterministic code in the launcher** does the work from the host; or
2. **A requirement in the sandbox CLAUDE.md** tells the session (and so the
   user) exactly what the repo needs, with a copy-paste snippet.
A self-contained, vendored file in the repo is allowed as an *optional* extra
(see "Where it runs"), never a pointer to this repo.

## Packaging
- Core is a stdlib-only `sandbox_check.py`; `claude_sandbox.py --check`
  delegates to it, skipping the update check, venv bootstrap, incus and any
  container paths. It reads `git log` from a checkout and needs no token.
- It reads commits from a local clone; the host launcher can use a throwaway
  `git fetch` of the PR head, CI uses a `fetch-depth: 0` checkout.

## Where it runs
**(1) Launcher-side, primary (deterministic, nothing added to the repo).**
- At session exit, before opening PRs, the launcher runs `--check` on each
  pushed branch. A failing branch still gets its PR (the work is on GitHub
  either way) but the PR is labelled `check-failed` and the findings are in
  its body and the exit summary.
- The launcher posts the result as a **commit status** on the branch head
  (context `sandbox/check`, `success`/`failure`, link to nothing sensitive)
  with the user's own `gh` login (needs `repo:status`; verify). The ruleset can
  then list `sandbox/check` as a required status check, **with no workflow in
  the remote repo at all**.
- This is also the hardest path to bypass: the logic and the status come from
  the host, outside what a PR can edit. A PR that wasn't checked has no status,
  so it can't merge (admins aside).
- Humans/other tools can run `claude_sandbox.py --check --post-status
  OWNER/REPO PR#` by hand for PRs not made by the launcher.

**(2) In the repo's own CI, optional, self-contained.**
- For repos that want it in Actions anyway, the launcher can render a
  *self-contained* `.github/workflows/sandbox-check.yml` plus a vendored copy
  of `sandbox_check.py` (with a header recording the launcher version and the
  file's sha256) and offer to add them through a normal PR. No `uses:` of this
  repo. The PR is held for review because it touches `.github/`.
- At each launch the launcher compares the vendored copy's hash with its own
  and warns when it has drifted ("vendored check is vN, launcher has vM; run
  `--check --update-vendored` to propose an update PR"). Drift detection is
  pure code with tests.
- Workflow shape: `pull_request` trigger (read-only token, no secrets), job
  `permissions: contents: read`, `actions/checkout` with `fetch-depth: 0`
  (pin by full SHA), then `python3 .sandbox/check.py`. The required check is
  that job's displayed name.
- Honest limit: this runs the PR's own version of the workflow and script, so a
  PR can edit them. Mitigations: required check (a removed job never reports and
  blocks merge), `.github/` and `.sandbox/` are on the launcher's held-for-review
  list, CODEOWNERS. Path (1) has no such weakness, which is why it is primary.

**(3) Requirement text in the sandbox CLAUDE.md.** A short section states the
repo-specific facts the session must tell the user: which check contexts exist
(`sandbox/check`, and the Actions job name if (2) is installed), that they
should be required in the ruleset and the exact ruleset snippet (the existing
required-checks section already has the mechanism), and that the vendored copy
needs updating when drift is reported.

## What `--check` validates
Per commit (range = merge-base with the base branch .. head):
- conventional prefix (`feat|fix|docs|test|ci|chore|refactor|perf|wip`);
- no merge commits (linear history), no fixup/squash leftovers;
- required trailer lines per repo rules: a `Verified:` or `Not verified:` line
  when the commit touches paths listed as needing real-environment runs (e.g.
  this repo's `sandbox_containers.py`, `sandbox_images.py`, `sandbox_deps.py`);
- `Follow-up:` lines are well-formed, and `Resolves follow-up from <sha>`
  names a real commit in history that has such a line;
- attribution trailer present when the session policy requires it;
- tests accompany code: warn when source files changed with no test change
  (heuristic, warn only);
- secret scan over added lines and messages (same patterns as plan 3).
Per PR as a whole:
- body has the template's required sections (plan 4), labels include
  `sandbox` and `session:<id>`; branch is under `sbx/<session>/`;
- **every `Follow-up:` in the PR's commits is listed in the PR body** and every
  `Not verified:` is surfaced, so a reviewer can't miss them;
- consistency: semantic contradictions (a body claiming "tests pass" while a
  commit says `Not verified:` for tests) can't be detected reliably, so don't
  try; instead require that the body's Verified and Not verified sections
  exist and reproduce every `Not verified:` line from the commits;
- size/scope: warn on large diffs spanning unrelated areas (heuristic);
- privileged paths touched are named in the body (they are held for review).
Rules and severities (`error` | `warn` | `off`) have defaults in the launcher
and an optional per-repo override file (`.claude_sandbox_check.yaml`, parsed
with a strict schema like the deps file; unknown keys are an error).

## Treat the inputs as hostile
Commit messages and PR bodies were written by a sandboxed agent. In workflows
never interpolate `${{ github.event.* }}` into `run:` (script injection); read
the event JSON file and `git log` from disk and pass data as arguments/env.
Use the `pull_request` trigger (read-only token, no secrets), never
`pull_request_target`, for the check.

## `--check --file-issues`
- Input: commits in `<before>..<after>` of the push (or `--since <sha>` /
  last-run marker manually). Scans the same record; produces proposed issues
  for (a) `Follow-up:` lines with no later `Resolves follow-up from <sha>`,
  (b) `Not verified:` lines on merged commits, (c) `Issue:` trailers,
  (d) `error` findings that reached `main` somehow.
- Every proposed issue goes through the plan-3 validators, carries the
  fingerprint marker for dedupe (search for the marker first), labels
  `sandbox` + `follow-up`, a link to the commit, caps per run (e.g. 20), and
  never `@`-mentions anyone.
- **Primary: launcher-side.** At session exit the launcher files issues for the
  session's own branches (plan 3 A), and `claude_sandbox.py --check
  --file-issues OWNER/REPO` can be run by hand over `main`'s recent commits with
  the user's `gh` login. No workflow needed.
- **Optional: self-contained workflow on `main`** (rendered and proposed by the
  launcher like the CI check above, no `uses:` of this repo): triggers
  `push: branches: [main]` (its definition comes from `main`, so only reviewed
  code runs), plus `workflow_dispatch` and optionally `schedule`.
  `permissions:` exactly `contents: read`, `issues: write`. GitHub mints and
  revokes `GITHUB_TOKEN`: no custody. `concurrency:` so runs don't race on
  dedupe.
- Manual run: `claude_sandbox.py --check --file-issues OWNER/REPO` with the
  user's `gh` login; `--dry-run` shows the issues it would file.
- When a later commit resolves a follow-up, the job **comments** on its issue
  (never closes or edits issues it didn't create; closing is left to a human,
  or opt-in `--close-resolved` for issues carrying its own marker).
- `GITHUB_TOKEN`-authored objects don't trigger other workflows; fine here
  (issues only).
- Everything is create-only plus comment on its own issues: the narrow verb set
  from plan 3, achieved here by the workflow's permissions rather than a broker.

## Relationship to plan 3 A
Same scanner and validators: at session exit the launcher runs the scanner on
the session's branches to file issues (A); in CI on `main` the workflow does it
for anything that slipped through or was merged without the launcher. Dedupe
markers make both safe together.

## Tests
- Fixture git repos built in `tmp_path`; each rule has a passing and failing
  history; severities and overrides; strict-schema rejection of bad config.
- Annotation/summary formatting; exit codes.
- Injection tests: commit messages containing `$(...)`, backticks, workflow
  command syntax (`::error::`), huge lines, invalid UTF-8; output is escaped
  and never executed.
- File-issues: dedupe on re-run, caps, resolved-follow-up detection, dry-run,
  fingerprint stability.
- Vendored-copy drift detection; rendered workflow is valid YAML, has no `uses:`
  of this repo, pins actions by SHA, and requests only the listed permissions.
- Commit-status posting: payload shape and context name (faked client).
- Not testable here: Actions behaviour, `repo:status` scope with the user's
  login, and required-status behaviour on a real ruleset; follow-up for a run
  on a throwaway repo.
