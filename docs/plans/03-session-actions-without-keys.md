# 3. Letting a session act on GitHub without holding a key

Goal: a sandboxed session can file issues and PRs, with **no key material in
the container and no new key material to custody or rotate on the host**.
Scope and validation matter more than the transport, so the plan is ordered
validators -> A -> (optional) C.

## Decision summary
| Option | Status | Key material |
|---|---|---|
| Validators (shared, pure) | **do first** | none |
| **A.** Session declares intents in git; the launcher acts on them | **do second** | none new: the host's existing `gh` login |
| **C.** Host-side broker over a socket | optional, later | none new |
| GitHub Actions `GITHUB_TOKEN` doing the filing | partly adopted: only for plan 5's post-merge job on `main` | none (GitHub mints/revokes it) |
| GitHub App installation tokens | **considered, won't do** (see end) | a `.pem` on the host |

## Why no key in the container
The agent has `sudo` in its own container, so anything in the container can be
read, and a token can only say "may write issues/PRs", not "may create but
never edit or close". Verbs exposed by the launcher *can* express that. Taking
the key out of the container also removes expiry, refresh and revocation
logic, and removes a leak path.

## Validators (the real asset; build once, reuse everywhere)
One stdlib-only module, `sandbox_forms.py`, imported by the launcher, by plan
5's checks, and (if built) by the broker. Pure functions, exhaustively tested:
- schema for an *intent*: `kind` (`issue` | `pr`), `title`, `body`, `labels`,
  and for a PR `head`/`base`;
- bounds: title one line <= 120 chars, body <= 20 KB;
- labels: always `sandbox` and `session:<id>`; any others only from an
  allowlist (defaults in the launcher, overridable per repo);
- strip/neutralise `@user` and `@org/team` so a session can't ping people;
  refuse closing keywords (`Fixes #n`) unless the intent explicitly allows;
- secret scan with refusal: `ghp_`, `github_pat_`, `gho_`, `sk-ant-`,
  private-key headers, long high-entropy tokens next to `token|secret|key`;
- PR rules: head under `sbx/<session>/` and pushed; base is the default branch
  or one of the session's own branches (same rule `sandbox_land` uses);
  privileged paths (`.github/`, `CODEOWNERS`, deps/host/setup files) cause a
  hold, using the one predicate `sandbox_land` already has (share, don't copy);
- per-session caps (e.g. <= 10 issues, <= 5 PRs) against spam loops;
- idempotency fingerprint: `sha256(session + kind + title + body)` embedded as
  `<!-- sandbox-intent:<fp> -->` in the created object, so a retry finds and
  reuses it instead of duplicating.

## A. Intents in git, launcher acts (no live channel)
Carriers, in order of preference:
1. **`Follow-up:` lines already in commit messages** (the repo convention) are
   intents for tracking issues. No new syntax.
2. **`Issue: <title>` trailer** on a commit; the commit body is the issue body.
   For when the session wants an issue that isn't a follow-up.
3. PRs: already automatic (`sandbox_land` opens one per pushed branch).

Flow: at session end, host-side, the launcher scans the session's pushed
commits (same scanner as plan 5), validates every intent with the validators,
and creates issues/PRs with the user's existing `gh` login, labelled and
templated (plan 4). Rejected intents are reported in the exit summary with the
reason, never silently dropped. This is plan 5's scanner pointed at the
session's branches instead of `main`.
- Limits: nothing happens mid-session; the session never sees the URL. It is
  told so in the sandbox CLAUDE.md ("write the follow-up in the commit; the
  launcher files it when you exit; URLs are in the exit summary").
- Authorship: issues/PRs appear as the human user, not a bot.
- Tests: scanner yields the right intents from fixture commit messages;
  trailer parsing edge cases; dedupe on re-run; caps; every rejection reason;
  exit summary lists accepted and rejected.

## C. Host-side broker (optional, only if live feedback is wanted)
A narrow RPC served by the launcher while the session runs:
`create_issue`, `open_pr` only. The in-container script (plan 4) sends JSON,
the launcher validates with the same validators and calls GitHub, then returns
the URL. Fixed verbs mean a compromised session can file a bounded number of
well-formed issues and nothing else: no edit, close, comment on others'.
- Transport: an incus `proxy` device carrying a unix socket from the container
  to the host (`bind=container`, believed supported; **unverified**, needs a
  real-incus test). Fallback with no socket: the launcher polls a spool
  directory in the container with `incus file pull`.
- Launcher change: today `session()` blocks in `subprocess.run`; the server
  needs a thread started before it and joined/stopped in `teardown()`.
  Fails closed: if the launcher dies the session simply loses the ability.
- The agent-facing scripts don't change from A to C except for the transport
  function.
- Tests: protocol framing, oversize/malformed requests, caps, server stops on
  teardown, socket path/permissions (0600, owned by the session's uid only).

## Considered, won't do: GitHub App installation tokens
Technically fine (repo-scoped, permission-scoped, <= 1 h, revocable), but it
needs a `.pem` private key on the host (manual custody, rotation, a file
that must be mode 600), a one-time App creation and per-repo install that only
the user can do, a host-side refresher for sessions > 1 h, and still puts a
usable token in a container where the agent has `sudo`, so it cannot enforce
the format. It gives up the custody-free options for little gain. Revisit only if a *bot identity* for authorship becomes a requirement or
the launcher must run without the user's `gh` login; record the decision in a
commit if so.

## Order of work
1. `sandbox_forms.py` + tests (no behaviour change).
2. A, with plan 5's scanner (they share it).
3. C only if wanted.
