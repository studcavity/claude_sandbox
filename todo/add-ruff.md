# Add ruff as the linter (replacing pyflakes)

## Problem
CI and `scripts/final-check.sh` lint with `pyflakes` only. That catches unused
imports and undefined names, but not import ordering, shadowed builtins,
unreachable code, common bug patterns or formatting drift. The package
refactor (spec 09) showed the cost: its mechanical rewrites left stray imports
and a local variable shadowing a module that only a hand-written script, not the
linter, could find.

## What was considered
Spec 09 PR 2 item 1 left `ruff` out on purpose: it is a new CI dependency, and
turning on more than pyflakes' rule set would produce a large one-off diff
that should not be mixed into a refactor. Nothing has been tried yet.

## Options
- **A. Ruff with only pyflakes' rules (`select = ["F"]`).** Same findings as
  today, one faster tool, no churn. Lets a later change widen the rules one
  family at a time. (Lean towards this as the first step.)
- **B. Add `I` (import order), `B` (bugbear), `UP` (pyupgrade), `A`
  (shadowed builtins).** Real value, but expect a sweeping diff; apply it as
  its own commit with the autofix and nothing else.
- **C. Also `ruff format`.** Largest diff and a style decision for the owner;
  skip unless wanted.
- **D. Do nothing.** pyflakes plus mypy plus the layering test already cover
  the cases that have broken so far.

## Where
- Add `ruff` to `requirements-dev.txt` (with a `>=` floor) and a
  `[tool.ruff]` section to `pyproject.toml`.
- Replace `python -m pyflakes ...` in `.github/workflows/tests.yml`,
  `scripts/final-check.sh` and `CLAUDE.md`'s test commands.
  `tests/test_ci_config.py` pins parts of the CI setup; update it with the
  change. The workflow change holds the PR for manual review.

## Done when
`ruff check claude_sandbox scripts tests` exits 0 locally and in CI on Python
3.12 and 3.14, pyflakes is no longer invoked anywhere, and the commands in
`CLAUDE.md` match what CI runs. Verify by introducing an unused import and
checking that both CI and `scripts/final-check.sh` fail.

## Risks
A CI job's name must not change (a renamed job strands open PRs if it is a
required check); only the lint step inside the existing job should change.
