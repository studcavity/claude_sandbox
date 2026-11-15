# 5. `--check`: validate the *work record*, in CI and after merge

Two modes of one verb, built on the shared validators and scanner (plan 3/4):

1. **`--check`** (read-only): run in a PR's CI. Validates every commit and the
   PR as one body of work. Exit 0/1, GitHub annotations, a job summary.
2. **`--check --file-issues`**: turns unresolved follow-ups and findings into
   tracked issues. Run manually, or on push to `main`, from a tightly scoped
   workflow.

## Naming
"lint" suggests style checks on source; this checks the *record of the work*
(commit messages, the PR as a unit, follow-ups), and "--fix" would suggest
rewriting things, which we must never do to pushed history. Proposed:
- **`--check`** (recommended): the conventional CI verb, plain.
  Second mode **`--file-issues`** instead of `--fix`: says what it does.
- Alternatives: `--audit` (fits "inspect the record"), `--vet`, `--verify`,
  `--conform`. Avoid `--review` (collides with the code-review skill) and
  `--gate` (describes a role, not an action).
Follows the CLI's convention of modes as flags (`--land-all`,
`--setup-token`); a positional REPOSITORY already exists so subcommands would
conflict.

## Packaging: runs without incus, without pip, without a token
- Core in a stdlib-only `sandbox_check.py`; `claude_sandbox.py --check` just
  delegates (skipping the update check, venv bootstrap, incus and gh login
  paths). CI can run `python3 sandbox_check.py` from a pinned checkout.
- Reuse in other repos via a **reusable workflow** in this repo, called with
  `uses: studcavity/claude_sandbox/.github/workflows/check.yml@<full SHA>`
  (pin to a commit, never a branch). Needs the launcher repo to be readable by
  the caller (public, or allowed for private repos in its Actions settings).
- The check job needs only `contents: read`; it reads `git log` from a
  `fetch-depth: 0` checkout and labels/body from the event JSON, so **no
  token beyond the default read-only one**.

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

## Making the check un-skippable
A `pull_request` workflow runs the version from the PR, so a PR could edit
the workflow to skip itself. Layers: (a) the check is a **required status
check** in the ruleset, so if the job is removed it never reports and the PR
can't merge; (b) the launcher already holds back PRs touching `.github/`, and
CODEOWNERS can require the user's review there; (c) the job is a call to the
pinned reusable workflow, so the logic isn't in the PR's reach. Note the
required check name in the sandbox CLAUDE.md's required-checks section.

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
- **Workflow scope**: separate workflow, triggers `push: branches: [main]` (its
  definition comes from `main`, so only reviewed code runs), plus
  `workflow_dispatch` and optionally `schedule`. `permissions:` is exactly
  `contents: read`, `issues: write`; nothing else. GitHub mints and revokes
  `GITHUB_TOKEN`: no custody. Uses `concurrency:` so runs don't race on dedupe.
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
- Not testable here: the reusable workflow on real Actions and the required-check
  behaviour; follow-up for a user run on a throwaway repo.
