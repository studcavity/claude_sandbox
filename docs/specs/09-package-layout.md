# Spec 09: a package layout instead of 30 flat modules

Source: user request (session agile-hamilton). Status: **PR 1 implemented**
(the move, branch `sbx/agile-hamilton/package-layout`); PR 2 (below) not
started, and **stacked on PR 1** (branch from it, not from `main`: both touch
the same lines, so the merge-conflict risk of parallel branches is high).
Spec 05 says stacked PRs get no CI (the workflows run only for PRs based on
`main`), so PR 2 is checked locally and re-based onto `main` once PR 1 merges. Settled with the user: grouped
subpackages, `scripts/sandbox_check.py` beside the shell scripts, `rulesets/`
under `config/`, a pure move first and the clean-up in a second PR.

## Problem

The repository root holds 30 `sandbox_*.py` modules, a 1,400-line entry script
(`claude_sandbox.py`) and about 15 other files. What belongs together is
invisible (the GitHub modules, the container modules, the stdlib-only modules
that also run in CI and inside the sandbox), the `sandbox_` prefix repeats in
every name, and the rule "the commit checker imports only stdlib-only
modules" is enforced by a hand-written test instead of by the directory
structure.

## Decisions

- **One package, `claude_sandbox/`** (not `sandbox/`: it matches the project
  and the old entry script, and the name is not generic). The `sandbox_`
  prefix is dropped inside it.
- **Grouped subpackages**, not a flat package:
  - `backends/` (base, github, local). The user's `backend/` idea, plural.
  - `container/` (everything about incus: the wrapper, images, deps, host
    file, volumes, pool).
  - `github/` (everything that talks to GitHub from the host: the `gh`
    wrapper, PyGithub client, rulesets, hardening, landing PRs, issues,
    labels, templates).
  - `portable/` (stdlib only, imports nothing outside `portable/`: the
    modules that also run in CI and inside the sandbox). Replaces a test that
    pinned a copy list to an import graph with a directory-level rule.
  - Launcher-wide modules stay at the package top.
- **`scripts/`** holds `smoke.sh`, `final-check.sh` and `sandbox_check.py`.
  **Deviation found while moving:** the launcher also imports the checker as
  a library (`backends/local.py` calls `check_findings`), so the logic lives
  in `claude_sandbox/portable/check.py` and `scripts/sandbox_check.py` is its
  thin command line, which finds the package by putting the repo root (the
  parent of its own directory) on `sys.path`. The sandbox gets all of
  `portable/`, so the old hand-kept copy list is gone.
- **`config/rulesets/`** replaces `rulesets/`.
- **`claude_sandbox/data/`** holds what the code reads at run time:
  `sandbox_CLAUDE.md`, `sandbox_ref_deps.md`, `templates/`. File names are
  unchanged. Paths come from one module, `claude_sandbox/paths.py`.
- **Entry point:** `run.sh` execs `python3 claude_sandbox/__main__.py`
  (still a bare exec). `__main__.py` replaces `sys.path[0]` with the repo root,
  runs the stdlib-only venv bootstrap, then calls `cli.main()`. The same file
  works as `python -m claude_sandbox`, and the bootstrap's re-exec into the
  venv needs no environment variables. The old `claude_sandbox.py` becomes
  `claude_sandbox/cli.py`, **unsplit** (splitting it is PR 2).
- **Stays at the root:** `README.md`, `CLAUDE.md`, `run.sh`,
  `claude_sandbox_deps.yaml` (read from the default branch by protocol),
  `todo/` (a protocol: CI looks there), `docs/`, `tests/`, `requirements*.txt`,
  `pytest.ini`, `mypy.ini`.
- **Mechanical, not clever.** PR 1 changes imports, paths and docs only. In
  module bodies the old names survive as import aliases
  (`from claude_sandbox.github import protect as sandbox_protect`), so no
  function body changes and no local variable can be shadowed (`deps`,
  `host`, `todo` are all used as variable names). Files move with `git mv` so
  `git log --follow` works.

## Old to new

| Old | New |
|---|---|
| `claude_sandbox.py` | `claude_sandbox/cli.py`, `claude_sandbox/__main__.py` |
| `sandbox_backend.py`, `_github`, `_local` | `backends/base.py`, `github.py`, `local.py` |
| `sandbox_containers.py` | `container/incus.py` |
| `sandbox_images`, `_deps`, `_host`, `_volumes`, `_pool` | `container/images.py`, `deps.py`, `host.py`, `volumes.py`, `pool.py` |
| `sandbox_github.py`, `_ghclient` | `github/gh.py`, `github/client.py` |
| `sandbox_protect`, `_hardening`, `_land`, `_issues`, `_labels`, `_templates` | `github/protect.py`, `hardening.py`, `land.py`, `issues.py`, `labels.py`, `templates.py` |
| `sandbox_names`, `_todo`, `_untrusted` | `portable/names.py`, `todo.py`, `untrusted.py` |
| `sandbox_bootstrap`, `_claude_config`, `_sessions`, `_provenance`, `_timing`, `_update`, `_util` | `bootstrap.py`, `claude_config.py`, `sessions.py`, `provenance.py`, `timing.py`, `update.py`, `util.py` |
| `sandbox_check.py` | `portable/check.py` + `scripts/sandbox_check.py` (entry) |
| `smoke.sh`, `final-check.sh` | `scripts/` |
| `rulesets/` | `config/rulesets/` |
| `sandbox_CLAUDE.md`, `sandbox_ref_deps.md`, `templates/` | `claude_sandbox/data/` |

## Things this changes beyond file names

1. **CI.** `tests.yml` (lint paths, shell scripts), `commit-metadata.yml` (the
   checker's path) and `file-issues.yml` (`python -m claude_sandbox.github.issues`)
   change, so the launcher holds this PR for the user's confirmation. The
   commit-metadata job checks out the *base* branch as its trusted copy, and
   the base has no `scripts/sandbox_check.py` until this merges: the step
   falls back to the old path when the new one is absent, or the PR could
   never pass its own check. The fallback is transitional (follow-up below).
   No job is renamed.
2. **The sandbox's copy of the checker** now mirrors the repo layout
   (`scripts/sandbox_check.py` plus `claude_sandbox/portable/`), so the same
   bootstrap works there.
3. **`rulesets/` has two roles** and this move only keeps one. The launcher's
   own `protect-main.json` is found through `paths.py`. But a repo can define
   rulesets as `rulesets/*.json` on its default branch, and the launcher
   offers them when you launch against that repo; this repo used that for its
   own three. After the move, launching against *this* repo no longer offers
   them (other repos are unaffected: the convention is unchanged). They can
   still be applied by hand with `gh api ... --input config/rulesets/<file>`,
   as the README documents. Decide in PR 2 (below).
4. **`NEEDS_REAL_RUN`** in the checker now matches full repo paths
   (`claude_sandbox/container/incus.py`, `images.py`, `deps.py`), not base
   names, and the pre-push hook matches the same paths.
5. **`claude_sandbox_deps.yaml`** (`check:` path, now `scripts/final-check.sh`)
   also changes, which holds the PR for review too.

## Verification

- `python -m pytest`, `pyflakes`, `mypy`, `shellcheck` on the new paths, all
  by exit status.
- A test that every subpackage and `portable/` obey their import rules.
- `run.sh` to `--help` through the real entry point, in the venv bootstrap
  and re-exec path.
- **Not verifiable here:** `./smoke.sh` (no incus in the sandbox). The commit
  that touches `container/` says so; run it on a host with incus.

## Follow-up: PR 2 (not started)

Record progress here, in the commit that does each item.

1. `pyproject.toml`: move pytest/mypy settings and dependencies into it, add a
   `[project.scripts]` entry (`claude-sandbox`), keep the pinned
   `check_untyped_defs` setting and its test. Consider `ruff` for pyflakes.
2. Split `cli.py` (1,419 lines): argument parsing, the `Sandbox` class, the
   launch lifecycle, with `main()` only wiring them.
3. Drop the `sandbox_` alias prefixes from module bodies (needs per-module
   care about shadowed locals).
4. Mirror the package in `tests/` (one directory per subpackage) if it reads
   better.
5. A layering test (or `import-linter`) for the whole graph, not just
   `portable/`.
6. Remove the commit-metadata fallback to `sandbox_check.py` at the old path
   once PR 1 is on `main`.
7. Decide the repo-defined rulesets question (3 above): read
   `config/rulesets/` as well, move this repo's three back to `rulesets/`, or
   accept applying them by hand. Reading a new path from untrusted repos is a
   trust-surface change, so it needs its own review.
8. Run `./smoke.sh` on a host with incus and record `Verified: ./smoke.sh`.
9. Rewrite the README module table and `CLAUDE.md` file references for any
   names PR 2 changes.
