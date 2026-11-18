# Switch the type checker from mypy to ty (Astral) for speed

## Problem
mypy is the slowest step in lint (`python -m mypy claude_sandbox scripts/*.py`,
and again inside `tests/test_ci_config.py::test_mypy_passes_on_the_repo`, so
every pytest run pays for it, plus `scripts/final-check.sh` and both CI Python
versions). ty is Astral's type checker, designed to be much faster, and would
sit naturally beside ruff (see `todo/add-ruff.md`).

## What the repo relies on from mypy (don't lose these)
- **Bodies of unannotated functions are checked.** Most of this code is
  unannotated. `pyproject.toml` sets `[tool.mypy] check_untyped_defs = true`,
  and `tests/test_ci_config.py` proves it still catches the bug that crashed
  every launch: `self.session = name` shadowing a `session()` method (mypy
  reports `Cannot assign to a method`). `CLAUDE.md` lists this under "Safety
  nets (don't weaken these)".
- `ignore_missing_imports` and `warn_unused_ignores`, and the `types-PyYAML`
  stub in `requirements-dev.txt`.

## Not yet tried
Nothing was run. ty was not installed or evaluated here, and its maturity and
rule coverage change quickly: check its current release status before relying on
it. Whether it reports the method-shadowing case above in unannotated code is the
first thing to test.

## Options
- **A. Replace mypy with ty** if it catches the shadowing example and is stable
  enough. Rewrite the pinned test to run ty on the same example and assert the
  diagnostic, so the safety net stays proven. (Lean towards this only if the
  check above passes.)
- **B. Run both for a while.** ty in CI and the pytest run for speed, mypy only
  in CI as the authoritative gate, until ty has proved itself. Costs a second
  config, but nothing is weakened.
- **C. Keep mypy** and only speed it up (its cache dir is currently disabled in
  the pytest run with `--cache-dir /dev/null`; enabling a cache in the
  developer loop may remove most of the cost).

## Where
`pyproject.toml` (`[tool.mypy]` becomes `[tool.ty]` settings), `requirements-dev.txt`
(`mypy`, `types-PyYAML`), `.github/workflows/tests.yml` (the "Types (mypy)"
step; a workflow change holds the PR for manual review, and no job should be
renamed), `scripts/final-check.sh`, `CLAUDE.md` (test commands and the
safety-net bullet) and `tests/test_ci_config.py` (three mypy tests).

## Done when
The type check runs with ty locally and in CI on Python 3.12 and 3.14 and exits
0; a test fails if the checker stops flagging a method shadowed by an instance
attribute in unannotated code (revert the setting to prove it); `CLAUDE.md`
names the new command; and the wall-clock saving is stated in the commit
message with how it was measured.

## Update: tried ty 0.0.105 (not adopted)
ty does **not** flag `self.session = name` shadowing a `session()` method when
`name` is unannotated: the value is `Unknown`, which is assignable to anything.
It reports `invalid-assignment` only when the parameter is annotated
(`name: str`). `--error all`, `--error unsound-assignment`, `conflicting-declarations`,
`invalid-attribute-access` and `invalid-assignment` did not change that, and ty
has no `check_untyped_defs` equivalent. On the repo it also reports 3 diagnostics
(`container/images.py` `on_stale` possibly None, `github/issues.py`
`api.writes` on a union). ruff was adopted separately (the pyflakes todo).
`scripts/final-check.sh` must not pass what CI fails, so mypy stays the gate.
Revisit when ty checks unannotated bodies the way mypy does; re-run the
shadowing example first.
