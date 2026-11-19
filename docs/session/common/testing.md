---
type: Reference
title: "Testing and quality gates"
description: "What tests and checks a session's work must come with; read before committing code or reporting that tests pass."
tags: [testing, quality]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: claude-md
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/data/sandbox_CLAUDE.md
    title: Previously in the session CLAUDE.md
---

Work comes with automated tests that **live in the repo**; a one-off check in this
session is lost when the sandbox ends.

- Follow the repo's test framework and conventions (its CLAUDE.md, README, CI
  config, `tests/`). If there is none, add a lightweight idiomatic one (for example
  pytest) and document how to run it.
- New behaviour gets a test. A bug fix gets a test that fails without the fix
  (revert the fix to prove it). Commit tests with the code they cover.
- Before committing: lint everything if that takes a few seconds, otherwise every
  changed file; run the tests for what you changed (the whole suite too, if it is
  fast). Never commit failing tests or weaken them to pass; if a failure is not
  yours, tell the user.
- Judge a run by its exit status, not the tail of its output (`set -o pipefail`, or
  `PIPESTATUS`). Never report "tests pass" without a zero exit.
- If something cannot be tested here (hardware, external services, interactive
  UIs), isolate the logic that can be, and say what needs manual verification.
- Record test tooling in the deps file ([Editing the deps file](deps-file.md)) or
  the repo's own dependency files.
- How tests run in CI, or that they do not, is in [CI and workflows](ci.md).

# Gates worth mentioning

Mention these in your final message when a repo lacks them. Propose each as its own
small branch, do not add them unasked, and never weaken one that exists
(`.github/` changes are held back for manual review):

- a type checker that reads unannotated code (mypy `check_untyped_defs = True`,
  pyright, TypeScript `strict`);
- a test of the real entry point with only the edges faked;
- a smoke test in a real environment, run in CI;
- strict shell: `set -euo pipefail`, CI shell `bash -euo pipefail {0}`; never pipe
  into `grep -q` under pipefail.
