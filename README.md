# claude_sandbox

Run a Claude Code session against your repository inside a throwaway
container, and get back pull requests instead of a changed machine.

```
./run.sh my-repo              # a bare name is looked up under --github-user
./run.sh --local ~/src/proj   # a repo that isn't on GitHub
```

When you exit, the container and its credentials are destroyed. What the
session pushed comes back as PRs. Nothing is merged for you.

## The problem

A coding agent is most useful when it can run commands, install tools and
push code without asking each time. That is also what makes it risky. On your
own machine it can reach your files, your credentials and every repo your
account can write to, and a prompt injection in a file it reads can turn that
reach against you. Approving every action is safe but defeats the point.

claude_sandbox lets the agent work unattended inside a boundary that is cheap
to throw away:

- **It can only lose its own work.** The container is destroyed on exit. On
  a GitHub session the only credential that can write anywhere is a deploy
  key scoped to one repo and revoked at teardown; a `--local` session has no
  network credential at all.
- **Nothing it writes is trusted until you read it.** Changes arrive as PRs
  that the launcher opens from your side (GitHub sessions) or as branches
  imported into your repo for review (`--local`). The session has no GitHub
  API token, cannot merge, and its commits are signed so you can tell which
  came from it.
- **Anything with extra privilege is held for review.** Branches that touch
  CI, hooks or the sandbox's own setup get no PR until you say so.

It is for developers who want to hand a repo to an agent and review the result
as they would a colleague's pull request. It does not make a malicious session
harmless: it limits the damage to one repo's unmerged branches and makes that
damage visible. The [threat model](docs/host/security/threat-model.md) says exactly
what it does and does not stop.

## Requirements

Linux host with Incus (your user in `incus-admin`), Python 3.12+, and for
GitHub sessions `gh` logged in as the account you launch as (without the
`workflow` scope). Whether a session is GitHub or local is chosen at each
launch, not by the repo: the same repo can be launched either way.
Details: [Getting started](docs/host/guides/getting-started.md).

## Where to go next

**Use it**
- [Getting started](docs/host/guides/getting-started.md): first launch, model, Claude login
- [Local repositories](docs/host/guides/local-repos.md): `--local`, bundles in and out, crash recovery
- [Concurrent sessions](docs/host/guides/concurrent-sessions.md): identity, branches, PRs, held branches
- [Follow-up issues](docs/host/guides/follow-up-issues.md): `todo/` files
- [Images, caching and disk](docs/host/guides/images-and-caching.md)
- [Mounts, USB and nested containers](docs/host/guides/host-access.md): including the `claude_sandbox_host.yaml` request file

**Configure a repo for it**
- [`claude_sandbox_deps.yaml`](docs/host/reference/repo-deps-file.md): tools installed before a session
- [The final check](docs/host/reference/final-check.md): what runs when you exit
- [The session's CLAUDE.md](docs/host/reference/sandbox-claude-md.md)

**Trust and security**
- [Threat model](docs/host/security/threat-model.md): what is and isn't protected
- [Protecting `main`](docs/host/security/branch-protection.md), [provenance](docs/host/security/provenance.md),
  [Actions hardening](docs/host/security/actions-hardening.md), [secret scanning](docs/host/security/secrets-scanning.md),
  [optional rulesets](docs/host/security/optional-rulesets.md)

**Work on it**
- [Architecture and package layout](docs/dev/architecture/package-layout.md)
- [Session docs](docs/dev/architecture/session-docs.md): what the in-container Claude is told, and how
- [Design decisions](docs/dev/decisions/index.md): specs 01-09 and why
- [Contributing and testing](CLAUDE.md)

`./run.sh --help` lists every flag. `docs/` is three OKF bundles, one per reader
(operator, in-container session, maintainer); see [`docs/README.md`](docs/README.md).
