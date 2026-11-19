# claude_sandbox

Host-side launcher that runs a Claude Code session in a throwaway Incus/LXD
container with a repo-scoped GitHub deploy key. `README.md` introduces it and indexes `docs/`;
`docs/dev/architecture/package-layout.md` has the module table and the layout rules
(import layering is a test: `tests/test_layering.py`); module
docstrings say what each file is for.

## Constraints that aren't obvious from the code
- `run.sh` only `exec`s `claude_sandbox/__main__.py`: no logic, subshells or external
  commands (a test enforces this).
- `claude_sandbox/portable/` is **stdlib-only and imports nothing outside
  itself**: it holds the commit linter (`check.py`, run in CI through
  `scripts/sandbox_check.py` and copied into the sandbox as `sandbox-check`)
  and what it needs. The launcher copies the whole directory, so a test
  enforces the rule; keep `claude_sandbox/__init__.py` empty. Code that
  writes to GitHub (`github/issues.py`) must stay out of `portable/`.
- Session text is untrusted: sanitise it with `portable/untrusted.py`, never paste
  it into GitHub raw.
- `provenance.py`: the container makes the signing key and signs every
  commit; only the public half is recorded, on the host, never in the repo.
  Verification is git's own (`%G?`/`%GF`) with the user's git config shut out
  and `gpg.ssh.program` pinned (a lying program makes git call any commit
  good; a test proves it).
- `timing.py` stages are explicit `with timing.stage(...)`
  blocks, never wrappers assigned onto the `Sandbox` instance (shadowing a
  method crashed every launch once).
- `claude_sandbox/data/sandbox_CLAUDE.md` is a `string.Template` rendered into the sandbox's
  `~/.claude/CLAUDE.md` (write `$$` for a literal dollar sign) and loaded into
  every session, so keep it small: a test caps its size (per backend: text
  that differs goes in `<!-- if github -->` / `<!-- if local -->` ...
  `<!-- end -->` blocks, see `select_variant`). Detail needed only
  sometimes goes in the on-demand session docs (`docs/session/`, shipped to the
  container; see `docs/dev/architecture/session-docs.md`).
  If a change makes anything it says stale, fix it in the same branch.
- GitHub: use `github/client.py` (PyGithub, typed results) for new code;
  `gh_api` in `github/gh.py` stays for raw REST dicts or exact file bytes
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
   driven with invoke (`Context().run`, as `github/gh.py` does for `gh`),
   not a hand-rolled client. Quote every argument (`shlex.join`), pass data on
   stdin, set a timeout, never let the tool prompt, and map its failures to our
   own error type.
3. **Build it** only when nothing suitable exists: a small module with a narrow
   documented interface and no dependencies on the rest of the launcher, with
   the reason in its docstring. (`container/deps.py` `fetch` and the deps YAML schema
   are in this category.)

## Testing
pytest in `tests/`, one file per module (`tests/test_<module>.py`, flat); shared
fixtures in `tests/conftest.py` (`FakeBox`, `make_args`, a fixed `now`).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -n auto        # parallel (pytest-xdist), ~6s (hermetic gh tests need gh + openssl); no lint or mypy, run those below
ruff check claude_sandbox scripts tests
python -m mypy claude_sandbox scripts/*.py   # pyproject.toml sets check_untyped_defs
shellcheck run.sh scripts/*.sh .githooks/*
```

Unit tests never touch real containers, GitHub or the network: they mock
`subprocess.run`, `run`, `gh_api`, invoke's `Context`, `urllib` and
`container/deps.py` `fetch`. Keep it that way. Lint and the whole suite are fast, so
run both before every commit and judge them by exit status (`pytest | tail`
hides a failure).

CI (`.github/workflows/tests.yml`) runs ruff, mypy, shellcheck and pytest
on Python 3.12 and 3.14 (the versions in Ubuntu 24.04 and 26.04 LTS), on pushes to `main` and PRs from this repo (not
forks). Its `integration` job installs incus and runs `tests/integration` for
real (failing if they are skipped), including `test_launch.py` as a "Smoke"
step: the real launch lifecycle in an Ubuntu container with only GitHub faked.
Locally, `./scripts/smoke.sh [owner/repo]` runs them, and a sandbox launched with
`--nesting` can too (omit the live-GitHub `gh` tests there: they need a logged-in `gh`,
which a sandbox lacks, so expect those 5 to fail). The full smoke run is the
end-to-end test but is expensive (minutes of image building and I/O), so avoid it
unless a change really needs it. They are opt-in (marker `incus`, `CS_INTEGRATION=1`).
`tests/integration/test_gh.py` drives the real `gh` against a local HTTPS
server (runs wherever `gh` and `openssl` exist); its read-only live-GitHub
group needs `CS_GH_LIVE=1`. Enable the pre-push warning with
`git config core.hooksPath .githooks`. The launcher also runs
`Container.self_check()` right after boot so a broken exec/stdin path fails
at once.

### Safety nets (don't weaken these)
`tests/test_ci_config.py` fails if one is dropped.
- `pyproject.toml` has `[tool.mypy] check_untyped_defs = true` (otherwise unannotated bodies are
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
- **Changes to `container/incus.py`, `container/images.py` or
  `container/deps.py` need a real-container run**: `./scripts/smoke.sh` on a host with
  incus, then `Verified: ./scripts/smoke.sh` in the commit message. A sandbox launched
  with `--nesting` has incus and can run it. Without nesting (no incus), write
  "Not verified against real incus" and tell the user to run it.
- New behaviour gets a test; test the logic that can break (validation,
  quoting and escaping, cache and freshness decisions, teardown guarantees,
  command construction) and skip trivial glue. If something needs incus, a real
  key or a TTY, extract the decision into a pure function and test that, and
  say in the commit what was checked by hand.
- Don't commit failing tests or weaken one to make it pass.
