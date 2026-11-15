# claude_sandbox

Host-side launcher that runs a Claude Code session in a throwaway Incus/LXD
container with a repo-scoped GitHub deploy key. `README.md` has usage and the
module table; module docstrings say what each file is for.

## Constraints that aren't obvious from the code
- `run.sh` only `exec`s `claude_sandbox.py`: no logic, subshells or external
  commands (a test enforces this).
- `sandbox_check.py` (the commit linter; also run in CI and copied into the
  sandbox as `sandbox-check`) is **stdlib-only**, and so is everything it
  imports at module level (`sandbox_actions`, `sandbox_labels`,
  `sandbox_names`, `sandbox_templates`, `sandbox_untrusted`): a test pins the
  list the launcher copies into the sandbox to that import graph.
- Session text is untrusted: sanitise it with `sandbox_untrusted`, never paste
  it into GitHub raw.
- `sandbox_provenance.py`: the container makes the signing key and signs every
  commit; only the public half is recorded, on the host, never in the repo.
  Verification is git's own (`%G?`/`%GF`) with the user's git config shut out
  and `gpg.ssh.program` pinned (a lying program makes git call any commit
  good; a test proves it).
- `sandbox_timing.py` stages are explicit `with sandbox_timing.stage(...)`
  blocks, never wrappers assigned onto the `Sandbox` instance (shadowing a
  method crashed every launch once).
- `sandbox_CLAUDE.md` is a `string.Template` rendered into the sandbox's
  `~/.claude/CLAUDE.md` (write `$$` for a literal dollar sign) and loaded into
  every session, so keep it small: a test caps its size. Detail needed only
  sometimes goes in an on-demand reference like `sandbox_ref_deps.md`.
- GitHub: use `sandbox_ghclient.py` (PyGithub, typed results) for new code;
  `gh_api` in `sandbox_github.py` stays for raw REST dicts or exact file bytes
  (PyGithub's requester mangles raw content).
- Keep I/O (subprocess, network, filesystem) at the edges and decisions in
  small pure functions. Anything built into a shell string goes through
  `shlex.quote`, or use `Container.root_argv` (no shell at all).

## Build versus buy
Prefer not to write what already exists. In order:
1. **A library** with widespread usage and active development. Add it to
   `requirements.txt` with a floor (`>=`), not a vendored copy. A niche or
   stalled package is a liability.
2. **A CLI tool on the host** with a stable interface (bugfix/minor updates),
   driven with invoke (`Context().run`, as `sandbox_github.py` does for `gh`),
   not a hand-rolled client. Quote every argument (`shlex.join`), pass data on
   stdin, set a timeout, never let the tool prompt, and map its failures to our
   own error type.
3. **Build it** only when nothing suitable exists: a small module with a narrow
   documented interface and no dependencies on the rest of the launcher, with
   the reason in its docstring. (`sandbox_deps.fetch` and the deps YAML schema
   are in this category.)

## Testing
pytest in `tests/`, one file per module (`tests/test_<module>.py`); shared
fixtures in `tests/conftest.py` (`FakeBox`, `make_args`, a fixed `now`).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest               # ~3s, includes a mypy run (hermetic gh tests need gh + openssl)
python -m pyflakes *.py tests/
python -m mypy *.py            # mypy.ini sets check_untyped_defs
shellcheck run.sh smoke.sh .githooks/*
```

Unit tests never touch real containers, GitHub or the network: they mock
`subprocess.run`, `run`, `gh_api`, invoke's `Context`, `urllib` and
`sandbox_deps.fetch`. Keep it that way. Lint and the whole suite are fast, so
run both before every commit and judge them by exit status (`pytest | tail`
hides a failure).

CI (`.github/workflows/tests.yml`) runs pyflakes, mypy, shellcheck and pytest
on Python 3.10, 3.12 and 3.13, on pushes to `main` and PRs from this repo (not
forks). Its `integration` job installs incus and runs `tests/integration` for
real (failing if they are skipped), including `test_launch.py` as a "Smoke"
step: the real launch lifecycle in an Ubuntu container with only GitHub faked.
Locally, `./smoke.sh [owner/repo]` runs them, and a sandbox launched with
`--nesting` can too. They are opt-in (marker `incus`, `CS_INTEGRATION=1`).
`tests/integration/test_gh.py` drives the real `gh` against a local HTTPS
server (runs wherever `gh` and `openssl` exist); its read-only live-GitHub
group needs `CS_GH_LIVE=1`. Enable the pre-push warning with
`git config core.hooksPath .githooks`. The launcher also runs
`Container.self_check()` right after boot so a broken exec/stdin path fails
at once.

### Safety nets (don't weaken these)
`tests/test_ci_config.py` fails if one is dropped.
- `mypy.ini` has `check_untyped_defs = True` (otherwise unannotated bodies are
  skipped and `self.x = ...` shadowing a method went unnoticed).
- Every shell script starts with `set -euo pipefail`, and CI's default shell is
  `bash -euo pipefail {0}`. Never pipe into `grep -q` under pipefail; the
  pre-push hook shows the safe pattern.
- `tests/test_lifecycle.py` runs the real `main()` with only I/O faked; extend
  it when you add a launch step.
- A bug fix needs a test that fails without it; revert the fix to prove it.

### Rules
- **Commit messages hold point-in-time state** (`Verified:`, `Not verified:`,
  `Follow-up:`); find open ones with `git log --grep='Follow-up'` and close
  them with `Resolves follow-up from <sha>`.
- **Changes to `sandbox_containers.py`, `sandbox_images.py` or
  `sandbox_deps.py` need a real-container run**: `./smoke.sh` on a host with
  incus, then `Verified: ./smoke.sh` in the commit message. In a sandbox (no
  incus), write "Not verified against real incus" and tell the user to run it.
- New behaviour gets a test; test the logic that can break (validation,
  quoting and escaping, cache and freshness decisions, teardown guarantees,
  command construction) and skip trivial glue. If something needs incus, a real
  key or a TTY, extract the decision into a pure function and test that, and
  say in the commit what was checked by hand.
- Don't commit failing tests or weaken one to make it pass.
