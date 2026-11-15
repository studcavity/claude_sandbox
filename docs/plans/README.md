# Implementation plans

Written down, **not yet implemented**. Each file is self-contained so a fresh
session can pick one up. Status is tracked here; update it in the commit that
changes it.

| # | Plan | Status | Depends on |
|---|------|--------|-----------|
| 1 | [Startup performance and `--timings`](01-startup-performance.md) | planned | none (do the `--timings` flag first: it measures everything else) |
| 2 | [Typed GitHub client](02-typed-github-client.md) | planned (PyGithub) | none; helps 1b and 5's filing step |
| 3 | [Session actions without keys](03-session-actions-without-keys.md): validators -> A (intents in git); broker deferred; App tokens rejected | planned | none |
| 4 | [Issue/PR content contract: templates + validated tools](04-issue-pr-tooling.md) | planned | 3's validators |
| 5 | [`--check` / `--check --file-issues`](05-check-verb.md) | planned (names decided) | 3's validators and scanner |

Already done (not plans): first-run dialogs pre-answered
(`sandbox_claude_config.py`), `--low-credits` and the credits section of the
sandbox CLAUDE.md.

## Decisions (settled by the user, 2026-11-15)
- Client library: **PyGithub**, the most used (plan 2).
- Verbs: **`--check`** and **`--file-issues`** (plan 5).
- Broker (plan 3 C): **deferred**, kept as a future option only.
- GitHub App tokens: **considered, won't do** (plan 3).
- No cross-repo reusable workflows or actions (see below).

Open: nothing needed from the user before building plan 5's `--check` core
and the shared validators, or the `--timings` flag.

## Ground rules that apply to all of them
- **No remote-repo dependency on this repo.** Never `uses:` a reusable
  workflow or action from `studcavity/claude_sandbox` (cross-repo access
  settings, private repos). Where that pattern would be tempting, either do it
  as deterministic code in the launcher (host side), or state it as a
  requirement with a copy-paste snippet in the sandbox CLAUDE.md. Self-contained
  files rendered by the launcher and proposed through a PR are fine.
- Follow `CLAUDE.md`: pure decisions, I/O at the edges, tests with each change,
  `Verified:` / `Not verified:` / `Follow-up:` lines in commits.
- Changes to `sandbox_containers.py`, `sandbox_images.py`, `sandbox_deps.py`
  need a real-container run (`./smoke.sh`) on a host with incus.
- Other sessions edit `claude_sandbox.py` `main()` and `sandbox_CLAUDE.md`
  concurrently. Keep new logic in new modules and wire it with the smallest
  possible diff; rebase before pushing.
