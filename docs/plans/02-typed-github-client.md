# 2. Typed GitHub client

## Premise check
There is **no official GitHub Python SDK** (GitHub's Octokit libraries are
JS/Ruby/.NET). Candidates, checked on PyPI 2026-11-15:

| | PyGithub 2.10.0 | githubkit 0.17.1 | ghapi 2.1.5 |
|---|---|---|---|
| last release | 2026-08-20 | 2026-11-06 | 2026-10-27 |
| model | wrapper objects (`Repository`, `PullRequest`, ...) | pydantic models generated from GitHub's OpenAPI spec | thin, dynamic |
| App auth | `Auth.AppInstallationAuth` | `AppInstallationAuthStrategy` | manual |
| maturity | long-standing, by far the widest usage | pre-1.0, expect breaking changes | small |

`CLAUDE.md` "build versus buy" asks for widespread usage plus active
development, so **recommend PyGithub**. If real pydantic types matter more than
stability, githubkit is the alternative; pin it with an upper bound and expect
churn. Whether PyGithub's hints are complete enough for mypy strict is
**unverified**: spike it (run mypy over the wrapper) before committing.

Note this reverses the current deliberate choice of the `gh` CLI (see the
comment in `sandbox_github.py`): the CLI gave us auth and patching for free.
Reasons to switch now: typed objects, one pooled HTTPS session (no process
spawn and TLS handshake per call: ~0.35 s each, ~11 per launch), real
concurrency (plan 1b), and the same client can mint App tokens (plan 3).

## Design
- `sandbox_github.py` keeps its public surface (`gh_api`, `github_login`,
  `repo_owner`, `validate_repo`, ...) at first so callers don't change, then
  callers move to small frozen dataclasses defined by us
  (`RepoInfo`, `Protection`, `DeployKey`, `PullRequest`, `Compare`) built from
  library objects at the edge. Library types never leak past this module, so a
  library swap stays a one-file change.
- **Auth stays on the host user's `gh` login**: obtain the token with
  `gh auth token` (a stable, documented command) and hand it to the client, so
  no new login UX and no token stored by us. Keep `GitHubError` and the
  existing status-code mapping; map the library's exceptions to it.
- Timeouts and retries set explicitly (client defaults differ); never prompt.
- Thread safety: confirm the chosen client's session is safe for the thread
  pool in plan 1b, otherwise one client per worker.

## Tests
- `tests/integration/test_gh.py` already drives the real `gh` against a local
  HTTPS server (`GH_HOST` + self-signed cert). Both libraries accept a custom
  base URL; repoint that harness at the new client so the same hermetic
  contract tests keep catching API-shape changes.
- Unit tests build library objects via the library's own test doubles or
  `responses`-style HTTP mocks, never the network.
- Delete the invoke/`gh` runner only after parity; `requirements.txt` gets the
  client with a floor (`>=`), and drop `invoke` if nothing else uses it.

## Rollout
1. Spike: mypy + one call each for login, repo, rulesets, keys, compare, pulls.
2. Swap internals behind the existing functions; all current tests pass.
3. Introduce the dataclasses caller by caller (one small commit each).
4. Remove the CLI path.
Risk: moderate (touches every GitHub interaction). Size: medium. Conflicts:
`sandbox_github.py` is edited by other sessions; rebase often.
