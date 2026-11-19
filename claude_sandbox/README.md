# claude_sandbox package

Source layout. Usage is in the repository's top-level `README.md`; the
decisions behind this layout are in `docs/specs/09-package-layout.md`.

Outside the package: `run.sh` (shim that execs `claude_sandbox/__main__.py`),
`scripts/` (`sandbox_check.py`, `smoke.sh`, `final-check.sh`), `rulesets/`
(GitHub rulesets), `tests/`, `docs/`, `todo/` (follow-up requests, filed as
issues by CI), `claude_sandbox_deps.yaml` (this repo's own sandbox deps).

## Rules of the layout
- **`portable/` is stdlib-only and imports nothing outside itself.** It is the
  code that also runs in CI and inside the sandbox (the commit checker and
  what it needs). The launcher copies the whole directory, plus the empty
  `claude_sandbox/__init__.py`, into the sandbox; a test enforces both rules.
  Keep `claude_sandbox/__init__.py` empty for the same reason.
- **`github/` is everything that talks to GitHub from the host.** Code that
  writes to GitHub (`github/issues.py`, `land.py`) stays out of `portable/`.
- **Locations come from `paths.py`**, never from `__file__` elsewhere.
- **Layering** (`tests/test_layering.py`): `container/` imports only the leaf
  modules (`paths`, `timing`, `util`, `portable/`); `github/` may also use
  `container/` and `sessions`; `backends/` may use those and `provenance`;
  nothing below `cli`, `sandbox` and `options` imports them.
- Modules are imported by their own name (`from claude_sandbox.github import
  protect`). Where that would clash with a local variable the import is
  aliased `<package>_<module>` (`container_deps`, `github_land`).

## Modules
| file | role |
| --- | --- |
| `__main__.py` | entry point (`run.sh` execs it; `python -m claude_sandbox` too): puts the repo root on `sys.path`, runs the venv bootstrap, calls `cli.main()` |
| `cli.py` | `main()` and the launch sequence (`_launch`), the background image refresh |
| `options.py` | command-line defaults, the argument parser, normalising and validating arguments |
| `sandbox.py` | `Sandbox`: one container's lifecycle (create, secrets, clone, session, teardown), backend choice, the USB prompt |
| `render.py` | render the session `CLAUDE.md` and the deps reference from their templates |
| `resume.py` | the final check's failure text and resume prompt, and running check output streams |
| `mergecheck.py` | the final check's built-in test that each session branch merges cleanly into its base (conflicts only; being behind is fine) |
| `shell.py` | the shell snippets run in the container (marker file, git config, secrets, the session command) |
| `session_tool.py` | install the commit checker in the sandbox as `sandbox-check` |
| `credentials.py` | the Claude OAuth token file and the secrets piped into a session |
| `paths.py` | where the checkout's directories are (root, `data/`, `rulesets/`) |
| `bootstrap.py` | stdlib-only: create `venv/`, install requirements, re-exec |
| `update.py` | offer to `git pull` when this checkout is behind |
| `sessions.py` | session ids (Docker-style names), branch prefix, stale-key reaping, protection check |
| `provenance.py` | per-session commit-signing keys (public half kept on the host) and the `sandbox/provenance` status |
| `skills.py` | Claude skills shipped to every session, kept as git submodules under `data/skills/` (updated on each launch; the launch stops only if the update fails and none are checked out) and copied into the container's `~/.claude/skills/` |
| `claude_config.py` | pre-answers Claude Code's first-run dialogs (theme, onboarding, API key, trust for repos you own) |
| `timing.py` | `--timings`: stages and call counts |
| `util.py` | logging, subprocess and signal helpers |
| `backends/base.py` | `Backend`: the parts of a launch that depend on where the code comes from (credential, clone, CLAUDE.md access text, landing) |
| `backends/github.py` | the GitHub backend: deploy key, clone over SSH, PRs and provenance, protection and Actions checks |
| `backends/local.py` | the local backend (`--local`): bundle in, bundle out, import into the host repo, review report, `--recover` |
| `container/incus.py` | `Container`: thin wrapper over `incus` (or `lxc`) |
| `container/images.py` | cached base/deps images, applying deps into a container |
| `container/deps.py` | parse/validate/download `claude_sandbox_deps.yaml` |
| `container/host.py` | parse/approve `claude_sandbox_host.yaml` (USB requests) |
| `container/volumes.py` | size-capped volumes over `/tmp` and the clone dir; sweep of leaked ones |
| `container/pool.py` | free-space check (and opt-in growth) of the incus storage pool |
| `github/gh.py` | GitHub access via the `gh` CLI (invoke), repo-name validation, concurrent prefetch of a launch's independent reads |
| `github/client.py` | typed GitHub operations through PyGithub (PR info, status, comments); auth is still your `gh` login |
| `github/protect.py` | offer and apply the default-branch rulesets (`rulesets/`) |
| `github/hardening.py` | checks (and, after a yes, tightens) the repo's Actions settings so session code's CI runs stay untrusted |
| `github/land.py` | open PRs for a session's branches (labelled, with the commits' `Not verified` lines), hold privileged ones |
| `github/issues.py` | files an issue for each `todo/` file a commit adds to the default branch (`python -m claude_sandbox.github.issues`; run by `file-issues.yml`) |
| `github/labels.py` | the labels on issues and PRs |
| `github/templates.py` | validated `data/templates/pr.md` and `issue.md` |
| `portable/check.py` | the commit linter (CI, and inside the sandbox as `sandbox-check`); `scripts/sandbox_check.py` is its command line |
| `portable/todo.py` | the `todo/<name>.md` follow-up request format (name, size, heading) |
| `portable/untrusted.py` | sanitising session text, and the credential scan |
| `portable/names.py` | session-name and branch-root rules shared with the checker |
| `data/sandbox_CLAUDE.md`, `data/sandbox_ref_deps.md` | templates for the sandbox's `~/.claude/CLAUDE.md` and `~/.claude/ref/deps.md` |
| `data/templates/` | PR and issue text |
