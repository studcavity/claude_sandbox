# 3. Per-session scoped GitHub token

Goal: let the sandboxed session create issues and open PRs itself, with a
credential that is limited to one repo, limited in what it can do, short-lived,
and revoked when the session ends.

## Mechanism: GitHub App installation tokens
Deploy keys only work for git over SSH, and fine-grained personal access tokens
**cannot be created by API**, so neither can be minted per session. A GitHub
App can: the launcher signs a short JWT with the App's private key and calls
`POST /app/installations/{id}/access_tokens` with
`{"repositories": ["<repo>"], "permissions": {...}}` to get a token that is
- restricted to the named repository (a subset of the installation's repos),
- restricted to the listed permissions (a subset of the App's),
- valid at most one hour,
- revocable early with `DELETE /installation/token`.
(These are GitHub's documented properties; re-check the current docs when
implementing.)

Permissions to request: `metadata: read` (implicit), `issues: write`,
`pull_requests: write`. **Not** `contents: write` (git pushes keep using the
per-session deploy key), not `workflows`, not `administration`.

## One-time setup (the user must do this)
Create a GitHub App (Settings -> Developer settings -> GitHub Apps) with only
those permissions, no webhook, "only on this account"; generate a private key;
install it on the repos to use. Store the key on the host at
`~/.config/claude_sandbox/app.pem` (mode 600) and the ids in
`~/.config/claude_sandbox/app.json` (`app_id`, optional `installation_id`;
otherwise look it up per repo with `GET /repos/{repo}/installation`). The
launcher must refuse to read a key file that is group/world readable.

## Launcher changes (new module `sandbox_app_token.py`, plan 2's client)
- Pure: `token_request(repo, read_only)` -> repositories + permissions
  (read-only sessions get `pull_requests: read`, `issues: read`, or no token);
  `needs_refresh(expires_at, now)`.
- Edge: `mint(repo)` -> `(token, expires_at)` (PyGithub:
  `GithubIntegration.get_access_token(installation_id, permissions=...)`;
  verify it supports restricting `repositories` in the pinned version, else
  call the REST endpoint directly); `revoke(token)`.
- Wire-up in `Sandbox`: mint after `provision_key`, deliver like the Claude
  credentials (0600 file in the container, sourced and removed by the session
  script, **never `incus exec --env`**: it would show on the host process
  list). Revoke in `teardown()` next to deploy-key deletion; teardown must not
  fail if revoke does (log it; the token expires within the hour anyway).
- **One-hour expiry vs long sessions**: a host-side refresher (thread in the
  launcher) re-mints at ~45 min and rewrites the file in the container, which
  the helper scripts (plan 4) re-read on each call. If the launcher dies the
  token simply expires: safe by default.
- Degrade gracefully: no App configured -> log one line, skip, sessions work
  as today (PRs still opened by the launcher at exit via `sandbox_land`).
- If the App isn't installed on the repo or lacks a permission, say exactly
  which in the log and the sandbox CLAUDE.md `$` note, never silently.

## What this does and doesn't protect
- A leaked token can only touch one repo, only issues/PRs, for <= 1 h.
- Anything in the container can read the token (the agent has sudo). So the
  token cannot enforce *how* issues/PRs are written (plan 4). Anything the
  token permits, a compromised session can do: comment on, edit or close any
  issue/PR in that repo, open spam PRs. Mitigate with repo-side CI/ruleset
  checks (plan 4) and the 1 h expiry.
- PRs and issues are authored by the App's bot user, not the human. That also
  means CI **does** run on its PRs (unlike `GITHUB_TOKEN`-authored ones).

## Alternative: host-side broker (stronger, recommended end state)
Instead of putting any token in the container, expose a narrow RPC (an incus
`proxy` device forwarding a unix socket, or `incus exec` stdin/stdout) served by
the launcher: `create_issue(title, body, labels)`, `open_pr(...)`. The launcher
holds the credentials (can even be the host user's own via plan 2), validates
every request (template, labels, size, secret scan, head branch must be under
`sbx/<session>/`, repo fixed), and calls GitHub. Enforcement becomes real
instead of conventional, and no expiry/refresh logic is needed. Cost: a
long-running server in the launcher, a socket protocol to design and test, and
the launcher must stay alive (it already must, to land PRs and revoke keys).
Plan 4's scripts are written against a tiny transport interface so they can
switch from "call GitHub with the token" to "call the broker" without changing
what the agent sees.

## Tests
- Pure request builder (read-only, repo scoping, no `contents`/`workflows`).
- Token file is 0600, not in `argv`, removed after sourcing, refreshed file
  replaced atomically.
- Teardown revokes and survives revoke failure; refresher stops on teardown.
- Hermetic contract test against the local HTTPS server harness for the
  access-token endpoint shape.
- Not testable here: a real App. Follow-up for the user: run once with a real
  App and `--timings` and confirm mint + revoke work, and that a token for repo
  A is rejected on repo B.
