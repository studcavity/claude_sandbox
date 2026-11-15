# claude_sandbox

Host-side launcher that runs a Claude Code session in a throwaway Incus/LXD
container with a repo-scoped GitHub deploy key. See `README.md` for usage and
the code-layout table.

## Code layout
- `claude_sandbox.py`: CLI (`build_parser`, `main`), the `Sandbox` lifecycle,
  and pure helpers (`render_claude_md`, `session_script`, `claude_secrets`,
  and so on).
- `sandbox_images.py`: cached base and deps images, `apply_deps`.
- `sandbox_containers.py`: `Container`, a thin `lxc`/`incus` wrapper.
- `sandbox_github.py`: GitHub REST client, token lookup, `validate_repo`.
- `sandbox_deps.py`: `claude_sandbox_deps.yaml` schema, downloads, archives.
- `sandbox_util.py`: `log`, `run`, `sigint_to_child`.
- `sandbox_CLAUDE.md`: `string.Template` rendered into the sandbox's
  `~/.claude/CLAUDE.md`. Write `$$` for a literal dollar sign.

Keep I/O (subprocess, network, filesystem) at the edges and put decisions in
small pure functions so they can be unit-tested without containers or network.
Anything built into a shell string must go through `shlex.quote`, or use
`Container.root_argv` (no shell at all).

## Testing
The test suite uses **pytest** and lives in `tests/`, one file per module
(`tests/test_<module>.py`). Shared fixtures are in `tests/conftest.py`: a
`FakeBox` container double, `make_args` for `Sandbox` arguments, and a fixed
`now`.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest               # all tests
python -m pyflakes *.py tests/ # lint
shellcheck run.sh              # shell lint (shellcheck is in claude_sandbox_deps.yaml)
```

The tests never touch real containers, GitHub or the network. They mock
`subprocess.run`, `run`, `gh_api`, `urllib` and `sandbox_deps.fetch` with
pytest's `monkeypatch`. Keep it that way so the suite stays fast and runs
anywhere. An end-to-end launch still needs a real host with incus and `gh`.
Do that by hand when changing container or image behaviour, and say so in
the commit message.

CI (`.github/workflows/tests.yml`) runs pyflakes, shellcheck and pytest on
Python 3.10, 3.12 and 3.13. It runs on every push to `main` and on PRs from
branches in this repo. PRs from forks are skipped.

### Real-container checks
Unit tests fake `subprocess`, so they can't catch bugs in how incus actually
behaves (e.g. `incus exec` stdin vs `/dev/stdin`). `tests/integration/` holds
opt-in tests marked `incus`; they skip unless `CS_INTEGRATION=1` and incus are
present. `./smoke.sh [owner/repo]` runs them (plus a real launch with a repo).
Enable the pre-push warning with `git config core.hooksPath .githooks`.
The launcher also runs `Container.self_check()` right after boot, so a broken
exec/stdin path fails immediately with a clear error.

### Rules
- **Changes to `sandbox_containers.py`, `sandbox_images.py` or
  `sandbox_deps.py` need a real-container run.** Run `./smoke.sh` on a host
  with incus and put `Verified: ./smoke.sh` in the commit message. If you are
  in a sandbox (no incus), say "Not verified against real incus" instead and
  tell the user to run it.
- **Every change comes with tests when feasible.** New behaviour gets a test.
  A bug fix gets a test that fails without the fix. Changes to
  `sandbox_CLAUDE.md` are covered by `test_render_claude_md_real_template`,
  which fails on any unsubstituted placeholder.
- **All tests and lint must pass before committing.** Run the commands above
  before every commit. Don't commit with failing or skipped-because-broken
  tests, and don't weaken or delete a test just to make it pass.
- **Be pragmatic, not exhaustive.** Test the logic that can break: validation,
  security-relevant quoting and escaping, cache and freshness decisions,
  teardown guarantees, and command construction. Skip trivial glue and
  re-testing the standard library. A few focused tests beat many brittle ones.
- If something genuinely can't be tested automatically (it needs incus, a
  real GitHub key or an interactive TTY), extract the decision logic into a
  pure function and test that. Say in the commit what was verified manually.
