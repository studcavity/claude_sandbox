# Implementation plans

Written down, **not yet implemented**. Each file is self-contained so a fresh
session can pick one up. Status is tracked here; update it in the commit that
changes it.

| # | Plan | Status | Depends on |
|---|------|--------|-----------|
| 1 | [Startup performance and `--timings`](01-startup-performance.md) | planned | none (do the `--timings` flag first: it measures everything else) |
| 2 | [Typed GitHub client](02-typed-github-client.md) | planned, needs a decision | none; helps 1b and 5's filing step |
| 3 | [Session actions without keys](03-session-actions-without-keys.md): validators -> A (intents in git) -> optional C (broker); App tokens rejected | planned | none |
| 4 | [Issue/PR content contract: templates + validated tools](04-issue-pr-tooling.md) | planned | 3's validators |
| 5 | [`--check` / `--check --file-issues`](05-check-verb.md) | planned, name open | 3's validators and scanner |

Already done (not plans): first-run dialogs pre-answered
(`sandbox_claude_config.py`), `--low-credits` and the credits section of the
sandbox CLAUDE.md.

## Decisions needed from the user
1. **Client library** (plan 2): PyGithub (recommended) or githubkit. There is
   no *official* GitHub Python SDK.
2. **Verb names** (plan 5): `--check` + `--file-issues` (recommended), or
   `--audit`, `--vet`, `--verify`.
3. **Live channel** (plan 3 C): wanted at all, or is exit-time filing (A)
   enough? C is optional.
4. **Bot identity**: only if you want authorship to be a bot rather than you
   would the rejected GitHub App option come back.

Settled: no key material is given to the container and no App key is held on
the host (plan 3).

## Ground rules that apply to all of them
- Follow `CLAUDE.md`: pure decisions, I/O at the edges, tests with each change,
  `Verified:` / `Not verified:` / `Follow-up:` lines in commits.
- Changes to `sandbox_containers.py`, `sandbox_images.py`, `sandbox_deps.py`
  need a real-container run (`./smoke.sh`) on a host with incus.
- Other sessions edit `claude_sandbox.py` `main()` and `sandbox_CLAUDE.md`
  concurrently. Keep new logic in new modules and wire it with the smallest
  possible diff; rebase before pushing.
