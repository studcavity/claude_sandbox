# Implementation plans

Written down, **not yet implemented**. Each file is self-contained so a fresh
session can pick one up. Status is tracked here; update it in the commit that
changes it.

| # | Plan | Status | Depends on |
|---|------|--------|-----------|
| 1 | [Startup performance and `--timings`](01-startup-performance.md) | planned | none (do the `--timings` flag first: it measures everything else) |
| 2 | [Typed GitHub client](02-typed-github-client.md) | planned, needs a decision | none; unblocks 1c and 3 |
| 3 | [Per-session scoped GitHub token](03-scoped-session-token.md) | planned, needs decisions | 2 |
| 4 | [Issue/PR tooling: scripts + templates](04-issue-pr-tooling.md) | planned | 3 (or 3's broker variant) |

Already done (not plans): first-run dialogs pre-answered
(`sandbox_claude_config.py`), `--low-credits` and the credits section of the
sandbox CLAUDE.md.

## Decisions needed from the user
1. **Client library** (plan 2): PyGithub (recommended) or githubkit. There is
   no *official* GitHub Python SDK.
2. **Token topology** (plan 3): token inside the container (what was asked,
   simplest, enforcement is only by convention + repo-side CI) vs a host-side
   broker (token never enters the container, enforcement is real). Recommended:
   ship the token variant first, design scripts so they can switch to the
   broker without changing the agent-facing interface.
3. **GitHub App**: plan 3 needs the user to create one GitHub App once and
   install it on their repos. Only the user can do this.

## Ground rules that apply to all of them
- Follow `CLAUDE.md`: pure decisions, I/O at the edges, tests with each change,
  `Verified:` / `Not verified:` / `Follow-up:` lines in commits.
- Changes to `sandbox_containers.py`, `sandbox_images.py`, `sandbox_deps.py`
  need a real-container run (`./smoke.sh`) on a host with incus.
- Other sessions edit `claude_sandbox.py` `main()` and `sandbox_CLAUDE.md`
  concurrently. Keep new logic in new modules and wire it with the smallest
  possible diff; rebase before pushing.
