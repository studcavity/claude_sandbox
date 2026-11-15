# Spec 06: one landing pipeline (and a review hold that depends on how you launched)

Source: not in `docs/plans/simplification.md`. Found while checking that
plan's item 5 against the code; it also absorbs that item's "PR record
section" and "fold `sandbox_labels` / `sandbox_templates`" cuts. Status: implemented. Problem 3: only `Not verified` lines stay in the PR body and `sandbox_record` was folded into `sandbox_land`; `sandbox_labels` / `sandbox_templates` still have two callers each and stay.
The first part is an inconsistency in a review safeguard. It is small and
worth its own PR, but it is not an execution hole (see below).

## Problem 1: whether `setup:` scripts are held depends on how the launcher was run

`sandbox_land.land(repo, session, extra_sensitive)` holds a branch (opens no
PR) when it touches a privileged path. `extra_sensitive` is the repo's
`setup:` script paths, which run as `ubuntu`, with the deploy key present,
in every future sandbox. The launcher computes that list from `self.deps`
(`claude_sandbox.setup_paths`), and `self.deps` is not always set:

- **`--land-all`** (in `claude_sandbox.main`) calls
  `sandbox_land.land(args.repo)` with **no** `extra_sensitive`. A branch that
  edits an existing `setup:` script (say `tools/setup.sh`) without touching
  `claude_sandbox_deps.yaml` gets a normal PR.
- **`--no-deps`**: `Sandbox.load_deps` returns early and leaves
  `self.deps = None`, so `setup_paths` is `[]`, with the same result for that
  session's branches.
- `self.deps` is also `None` when the deps file parses to nothing
  (`deps if lines else None`). Check whether a file with only `setup:`
  entries produces summary lines. If it doesn't, that case is affected too.

**How serious this is.** Opening the PR runs nothing. `setup:` scripts run
only in `Sandbox.run_setup`, at a later launch, from a clone of the default
branch. So a changed script runs only after a human has merged it. The
`.github/` hold prevents execution, because pushing or opening a PR would run
workflow code. The `setup:` hold makes the review explicit: a change that
will later run with the deploy key doesn't arrive as an ordinary-looking PR
that gets approved without a close look. Skipping it under some flags is
inconsistent and weakens that safeguard. It does not let unreviewed code
run.

**Done when:** the held set doesn't depend on launch flags or on which code
path lands. One way to get there: `land` reads `setup:` paths from the
default branch's deps file itself, at landing time. It is worth
considering that the session's branch could also *add* a setup entry. That
change touches the deps file and is already held, but say so in a test.
Add a test that fails without the fix, for both `--land-all` and
`--no-deps`.

## Problem 2: the post-landing steps are written twice

`Sandbox.land` and the `--land-all` branch of `main` each run the same
sequence: land → `sandbox/check` statuses → `sandbox/provenance` →
follow-up actions. Each has its own flag checks. They already disagree:

- `Sandbox.land` wraps each step in its own `try`, so one failing step never
  stops the next. In `--land-all`, an exception from the status step (for
  example a `GitHubError` from `gate_required`) skips provenance and
  actions, and returns 1.
- The `extra_sensitive` difference above.

Every spec that changes these steps (01, 03, 05) would otherwise have to
edit both copies, and keep them in step.

**Done when:** one function runs the post-landing sequence, and both entry
points call it. It has the same failure isolation for every step, and
`tests/test_lifecycle.py` (or a focused test) covers it from both entry
points. Doing this after spec 01 and spec 03 leaves less to merge, and
doing it before them means they edit one place. Either order works; pick
whichever lands first.

## Problem 3: PR body and the small helper modules

These are smaller, so a judgement call rather than a requirement:

- **"What the commits say" section** (`sandbox_record`,
  `sandbox_land.record_section`, `safe_line`). Every line in it is
  session-written text, which needs sanitising and a secret check before it
  goes out under the user's name. Its "Issues the launcher will file" block
  goes away with spec 02 anyway. Options: keep only `Not verified` (the
  caveats a reviewer most needs to see); or replace the section with a link
  to the commit list; or keep it as is. If you remove it, `sandbox_record`
  and its 13 tests go with it.
- **`sandbox_labels` / `sandbox_templates`.** After spec 01 removes
  `CHECK_FAILED`, and spec 03 moves issue filing, each may have one caller
  left. Fold a module into its caller when that happens. Until then they
  earn their keep, so don't fold early.

## Verify

- `python -m pytest`, `pyflakes`, `mypy`.
- Problem 1: revert the fix and watch the new test fail (`CLAUDE.md` rule).
- `rg -n "sandbox_land.land\(" claude_sandbox.py` shows a single call site.
