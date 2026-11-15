# Spec 08: a local-repo backend (bundle in, bundle out)

Source: user request (session gentle-germain). Status: in progress. Settled
with the user: option B for the workspace (incus volume, bundles in and out),
branches imported as `sbx/<session>/*`, networking unchanged.

## Problem

The launcher only works with GitHub: the repo is cloned over SSH with a
per-session deploy key, and work leaves the sandbox by `git push` and comes
back as PRs. Code that is not on GitHub, or whose remote the sandbox must not
reach (other forges, VPN-only hosts, repos where a deploy key is not allowed),
can't be used. We want a **local** mode: the user names a git repository on
the host, the session works on a copy, and its branches come back for review,
while the **primary repository is protected** (the session can't touch its
refs, config, hooks or working tree, and nothing it writes runs on the host).

## Options considered for the workspace

- **A. `git worktree` of the primary, bind-mounted.** Rejected. A worktree's
  `.git` is a pointer into the primary's `.git` (objects, refs, `config`,
  `hooks/`). To commit, the container needs that directory read-write, so it
  could move `main`, or set `core.hooksPath` / `core.fsmonitor` and get code
  run by the user's next `git status` on the host.
- **B. Incus volume, bundles in and out (chosen).** The host makes a `git
  bundle` of the base branch from the primary (trusted: it is the user's own
  repo) and streams it into the container, which clones it onto the
  size-capped work volume. At the end, the container streams a bundle of its
  `sbx/<session>/*` branches out, and the host fetches that bundle into the
  primary. No bind mount, so no uid-mapping decision (`raw.idmap` /
  `shift=true`) and the volume quota still applies. Cost: no live view of the
  files on the host, and the whole base history is copied in at launch.
- **C. A standalone clone in a host staging dir, bind-mounted.** Gives a live
  view and crash durability, but the dir has no quota, needs a uid mapping
  (each with trade-offs), and the host must never run git in it (its config
  is untrusted). Not chosen.

## Why bundles are the trust boundary

A bundle is data: a pack plus a list of refs. `git fetch <file.bundle>` runs
no hooks, reads no config from the producing repo and starts no
`upload-pack` in it, unlike a fetch from a local path, which would run
`git-upload-pack` with the untrusted repo's config. With
`transfer.fsckObjects=true` it is the same trust level as fetching from
GitHub. The host never runs git inside anything the session wrote.

## Design

### Refactor: a `Backend` the `Sandbox` delegates to

Everything GitHub-specific in `claude_sandbox.Sandbox` and `_launch` moves
behind one small interface. Proposed, not binding:

| hook | GitHub backend | local backend |
| --- | --- | --- |
| `label`, `repo_id` | `owner/name` | `local/<basename>-<8 hex of realpath>`; its signing keys live under a separate key-store root, so it can't collide with a GitHub owner named `local` |
| `check_host()` | gh login, `--github-user` match, workflow-scope warning | path is a git repo; base ref resolves to a commit; warn if the tree is dirty (only commits go in) |
| `prefetch_plan()` | today's list | `[]` |
| `read_file(path)` | `contents` API on the default branch | `git -C <primary> show <base-sha>:<path>` (missing = `None`) |
| `preflight()` | protection, Actions hardening, stale-key reaping | nothing |
| `provision(box)` | SSH key, deploy key, pinned `known_hosts` | nothing (no network credential at all) |
| `populate(box, workdir)` | `git clone` over SSH | bundle in (below) |
| `claude_md` facts | access lines, "push to GitHub" text | access lines, "commit; the launcher imports at exit" text |
| `trusted()` | owner == gh login | `True` (the user named the path) |
| `export(box, workdir)` (container alive) | no-op | bundle out (below) |
| `revoke()` (in teardown) | delete the GitHub key | no-op |
| `land()` (after teardown) | PRs and provenance (today's `land_and_report`) | import, then local provenance and checker report |
| `recover()` | `--land-all` | `--recover` (below) |

Shared and unchanged: images, deps install, volumes (except the keep rule
below), USB, git config, the signing key, setup scripts, session tools,
secrets, the session itself. Account-key mode stays inside the GitHub backend
(`repo=None`). `--setup-token` needs no backend.

The base class is an `abc.ABC` with no-op defaults where one backend has
nothing to do. The goal of the refactor commit is **no behaviour change**:
`tests/test_lifecycle.py` passes unchanged.

### CLI

- `--local PATH`: mutually exclusive with `REPOSITORY`/`--repo`/
  `--account-key`. `PATH` is resolved with `realpath`, and the repo root is
  found with `git rev-parse --show-toplevel`.
- `--base REF` (local only): the branch to start from. The default is the
  primary's current branch; it is an error if `HEAD` is detached and no
  `--base` is given.
- `--recover` (local only): import what crashed launches left behind, then
  exit.
- GitHub-only flags (`--read-only`, `--no-pr`, `--land-all`, the prompt
  flags, `--github-user` checks) are rejected or ignored with `--local`; which
  one is decided per flag and tested.

### Bundle in

1. Host: `git -C <primary> bundle create - <base-sha-as refs/heads/<base>>`.
   Only that one ref goes in, so other local branches (possibly private) are
   not copied. The stream goes to the container over stdin (as `put_file`
   does, never through a command line) into
   `~/.local/share/claude-sandbox/base.bundle`.
2. Container: `git clone base.bundle <workdir>` onto the work volume, then
   `checkout -b sbx/<session>/work`. `origin` stays the bundle file, so
   `origin/HEAD` and `origin/<base>` resolve and `sandbox-check` works (its
   `git fetch` re-reads the same bundle).

### Bundle out (container alive, after the session exits, before teardown)

`git -C <workdir> bundle create - --branches='sbx/<session>/*' ^<base-sha>`,
run as the session user with a timeout and a size cap, streamed to
`<state>/local/<repo_id>/<session>.bundle` on the host (mode 0600). A process
the session left running can't change what is imported: the import reads only
this file. If export fails (no branches, timeout, too big), the work volume is
kept for `--recover`.

### Import (host, after teardown)

1. `git bundle list-heads <file>`: every ref must be
   `refs/heads/sbx/<session>/<name>` and pass `git check-ref-format`; anything
   else is dropped with a warning (never fetched).
2. `git bundle verify` against the primary (its prerequisite is the base
   commit, which the primary has).
3. `git -C <primary> -c transfer.fsckObjects=true fetch --no-tags <file>
   +refs/heads/sbx/<session>/<n>:refs/heads/sbx/<session>/<n> ...`, with one
   explicit refspec per validated ref, never a wildcard into another
   namespace. git itself refuses to update a branch that is checked out.
4. On success, delete the bundle and the kept volume.

### Review report (host, advisory; nothing is executed)

For each imported branch, `<base-sha>..<tip>`:
- provenance: `sandbox_provenance.read_commits` + `judge` with the session's
  recorded key (key store id = `repo_id`);
- the commit checker: `sandbox_check.check_findings` with `cwd=<primary>`;
- privileged files: `sandbox_land.sensitive_paths`, plus `setup:` paths from
  the base's deps file, shown as **warnings** (locally nothing runs on
  import, so there is nothing to hold);
- the `PR-Title` synopsis and `Not verified` lines (from `sandbox_land`).

Printed like today's landing box, ending with `git log -p
<base>..sbx/<session>/work`. Merging is the user's: the launcher never touches
any ref outside `sbx/<session>/`.

### Keeping work across a crash

Today `/home/ubuntu/<repo>` sits on a custom volume named
`claude-sandbox-vol-<ct>-work`, and the sweep deletes unused ones older than
an hour. In local mode the work volume gets a distinct prefix
(`claude-sandbox-keep-<repo_id-ish>-<session>`) that the sweep never matches,
and teardown does not delete it until the import succeeds. Without a volume
(`--volume-size 0`, or a pool without quotas), a crash loses the work, as an
unpushed clone does today; the session is told which case it is in.

`--recover`: for each kept volume of this repo, launch a throwaway container
from the cached base image with the volume attached **read-only**, run the
bundle-out step there, destroy it, then import as above.

### Session text

`sandbox_CLAUDE.md` is mostly GitHub-specific in a few sections (access,
"protect your work", branches/PRs, CI, reaching the default branch). Those
become placeholders filled per backend from small partials, so the shared text
isn't duplicated. The size test keeps covering the rendered result for both
backends. Local text says: there is no remote and no network credential;
commit often (commits survive on the volume if there is one); work only on
`sbx/<session>/*`; the launcher imports those branches into the user's repo at
exit; there is no PR and no CI.

## Not in scope

- Egress restrictions (the user said networking stays as it is).
- Live host view of the files (option C).
- Shallow or partial bundles for very large repos (follow-up if needed).
- Non-git source directories.

## Plan (one commit or more per step, each pushed)

1. This spec.
2. Refactor: `Backend` ABC + `GitHubBackend`; `Sandbox` delegates; no behaviour
   change.
3. Pure local helpers + tests: `repo_id`, bundle ref validation, refspecs,
   bundle in/out argv.
4. `LocalBackend` + `--local`/`--base`, bundle in, session text, lifecycle test.
5. Bundle out, import, review report; kept volume.
6. `--recover`.
7. README, CLAUDE.md, an incus integration test (`tests/integration`).

## Done when

- `./run.sh --local ~/src/proj` launches, the session commits on
  `sbx/<s>/work`, and on exit that branch is in `~/src/proj` with a report;
  `main` and the working tree of `~/src/proj` are untouched.
- A bundle carrying `refs/heads/main` or `refs/heads/sbx/<other>/x` imports
  nothing for those refs (unit test, plus a real-git test with a crafted
  bundle).
- A container killed mid-session leaves a kept volume, and `--recover`
  imports its branches.
- GitHub mode behaves as before (the existing suite, including
  `test_lifecycle.py`, unchanged).
- Verified against real incus with `./smoke.sh` (or stated as not verified).

## Risks

- Changes to `sandbox_volumes.py` and the container code need a real-incus
  run (CLAUDE.md rule). This sandbox may not be able to provide one.
- Bundle size: the base history is copied in full. Very large repos are slow
  at launch; the size cap on export must stay generous.
- The local key store id must not collide with a GitHub `owner/name`
  (`local/` is a valid GitHub owner name, so it is namespaced by a separate
  directory, not just a prefix).
