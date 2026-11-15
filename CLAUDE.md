# claude_sandbox

Host-side launcher that runs a Claude Code session in a throwaway Incus/LXD
container with a repo-scoped GitHub deploy key. See `README.md` for usage and
the code-layout table.

## Code layout
- `claude_sandbox.py`: CLI (`build_parser`, `normalize_args`, `main`), the
  `Sandbox` lifecycle, and pure helpers (`render_claude_md`, `session_script`,
  `claude_secrets`, `check_expected_user`, and so on). Holds the defaults
  (github user, git email, image, cli).
- `sandbox_bootstrap.py`: stdlib-only; creates `venv/` from `requirements.txt`
  (re-installing only when its hash changes) and re-execs into it.
- `sandbox_update.py`: "this checkout is behind upstream, `git pull`?" check.
- `run.sh`: a thin shim that only `exec`s `claude_sandbox.py`. Keep it that
  way: no logic, no subshells or external commands (a test enforces this).
- `sandbox_images.py`: cached base and deps images, `apply_deps`.
- `sandbox_containers.py`: `Container`, a thin `lxc`/`incus` wrapper.
- `sandbox_github.py`: GitHub access through the host's `gh` CLI (run with
  invoke), `validate_repo`, `resolve_repo`.
- `sandbox_host.py`: `claude_sandbox_host.yaml` (USB requests) and approval.
- `sandbox_deps.py`: `claude_sandbox_deps.yaml` schema, downloads, archives.
- `sandbox_util.py`: `log`, `run`, `sigint_to_child`.
- `sandbox_check.py`: the commit linter (also run in CI, and copied into the
  sandbox as `sandbox-check`), `--post-status` / `--override` (the
  `sandbox/check` commit status), `--file-issues`. **Stdlib-only**, and so is
  everything it imports at module level (`sandbox_actions`, `sandbox_labels`,
  `sandbox_names`, `sandbox_templates`, `sandbox_untrusted`): a test pins the
  list the launcher copies into the sandbox to that import graph.
- `sandbox_actions.py`: `Sandbox-Action` trailers -> issues (spec in the
  docstring); `sandbox_record.py`, `sandbox_untrusted.py`, `sandbox_labels.py`,
  `sandbox_templates.py` (+ `templates/`), `sandbox_names.py`: small pure
  helpers. Session text is untrusted: sanitise with `sandbox_untrusted`, never
  paste it into GitHub raw.
- `sandbox_timing.py`: the `--timings` stopwatch; stages are explicit
  `with sandbox_timing.stage(...)` blocks, never wrappers assigned onto the
  `Sandbox` instance (shadowing a method crashed every launch once).
- `sandbox_CLAUDE.md`: `string.Template` rendered into the sandbox's
  `~/.claude/CLAUDE.md`. Write `$$` for a literal dollar sign.

Keep I/O (subprocess, network, filesystem) at the edges and put decisions in
small pure functions so they can be unit-tested without containers or network.
Anything built into a shell string must go through `shlex.quote`, or use
`Container.root_argv` (no shell at all).

## Build versus buy
Prefer not to write what already exists. In order:
1. **A library**, if it has widespread usage and active development (recent
   releases, maintained issue tracker). Add it to `requirements.txt` with a
   floor (`>=`), not a vendored copy. Check both before adding; a niche or
   stalled package is a liability we would end up maintaining.
2. **A CLI tool on the host**, if its interface is stable and its updates are
   bugfix/minor releases rather than major ones. Drive it with invoke
   (pyinvoke: `Context().run`, as `sandbox_github.py` does for `gh`), not with a
   hand-rolled client. The host's package manager keeps the tool patched, which
   a library we pin or vendor would not be. Quote every argument
   (`shlex.join`), pass data on stdin, set a timeout, never let the tool
   prompt, and map its failures to our own error type so callers don't parse
   its output.
3. **Build it ourselves** only when no candidate is suitable, and then keep it
   minimal yet extensible: a small module with a narrow, documented interface
   and no dependencies on the rest of the launcher, as if preparing to split it
   out as its own library. Say in the module docstring why nothing existing
   fit. (`sandbox_deps.fetch` and the deps YAML schema are in this category.)

## Testing
The test suite uses **pytest** and lives in `tests/`, one file per module
(`tests/test_<module>.py`). Shared fixtures are in `tests/conftest.py`: a
`FakeBox` container double, `make_args` for `Sandbox` arguments, and a fixed
`now`.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest               # all tests (~3s; includes a mypy run) (hermetic gh contract tests need gh + openssl)
python -m pyflakes *.py tests/ # lint
python -m mypy *.py            # types; mypy.ini sets check_untyped_defs (see below)
shellcheck run.sh smoke.sh .githooks/*   # shell lint (shellcheck is in claude_sandbox_deps.yaml)
```

The tests never touch real containers, GitHub or the network. They mock
`subprocess.run`, `run`, `gh_api`, invoke's `Context`, `urllib` and
`sandbox_deps.fetch` with
pytest's `monkeypatch`. Keep it that way so the suite stays fast and runs
anywhere. An end-to-end launch still needs a real host with incus and `gh`.
Do that by hand when changing container or image behaviour, and say so in
the commit message.

CI (`.github/workflows/tests.yml`) runs pyflakes, mypy, shellcheck and pytest on
Python 3.10, 3.12 and 3.13. It runs on every push to `main` and on PRs from
branches in this repo. PRs from forks are skipped.
A second job, `integration`, installs incus on the runner and runs
`tests/integration` for real (it fails if they are skipped), so
container-behaviour regressions are caught in CI even without a nested sandbox.
It also runs `tests/integration/test_launch.py` as its own "Smoke" step: the
launcher's real lifecycle (launch, key, clone, CLAUDE.md, session, teardown)
in a real Ubuntu container with only GitHub faked.

### Safety nets (don't weaken these)
Each was added after a bug got through without it; `tests/test_ci_config.py`
fails if one is dropped.
- `mypy.ini` has `check_untyped_defs = True`. Without it mypy skips unannotated
  bodies and misses `self.x = ...` shadowing a method (it crashed every launch).
- Every shell script starts with `set -euo pipefail`, and CI's default shell is
  `bash -euo pipefail {0}` (GitHub's default has no pipefail, so
  `pytest | tee out` would pass on a failing pytest). Never pipe into `grep -q`
  under pipefail; the pre-push hook shows the safe pattern.
- `tests/test_lifecycle.py` runs the real `main()` with only I/O faked; extend
  it when you add a launch step.
- A bug fix needs a test that fails without it; revert the fix to prove it.

### Real-container checks
Unit tests fake `subprocess`, so they can't catch bugs in how incus actually
behaves (e.g. `incus exec` stdin vs `/dev/stdin`). `tests/integration/` holds
opt-in tests marked `incus`; they skip unless `CS_INTEGRATION=1` and incus are
present. `./smoke.sh [owner/repo]` runs them (plus a real launch with a repo).
Launch a sandbox with `--nesting` (this repo's deps install `incus`) and the
integration tests can run inside it; without `--nesting` they skip or fail at
container start.
`tests/integration/test_gh.py` (marker `gh`) drives the **real `gh` binary**
through `sandbox_github`. Its main group is hermetic: gh is pointed at a
throwaway local HTTPS server (`GH_HOST` plus a self-signed cert via
`SSL_CERT_FILE`), so it needs no login or network and catches changes in gh's
flags or output text. It runs in the normal suite wherever `gh` and `openssl`
exist (this repo's deps install `gh`, never logged in; CI runners have it) and
skips otherwise. A second group makes read-only calls to real GitHub and is
opt-in with `CS_GH_LIVE=1` (set by `./smoke.sh` and by CI with `GH_TOKEN`).
Enable the pre-push warning with `git config core.hooksPath .githooks`.
The launcher also runs `Container.self_check()` right after boot, so a broken
exec/stdin path fails immediately with a clear error.

### Rules
- **Commit messages are where point-in-time state lives**: what was and
  wasn't verified, and follow-ups that were necessary but couldn't be actioned
  (no incus here, needs the host, needs a real GitHub key). Use `Follow-up:`,
  `Verified:` and `Not verified:` lines. Look for open ones with
  `git log --grep='Follow-up'`, and close them in the commit that resolves
  them (`Resolves follow-up from <sha>`). Don't rewrite pushed history to edit.
- **Changes to `sandbox_containers.py`, `sandbox_images.py` or
  `sandbox_deps.py` need a real-container run.** Run `./smoke.sh` on a host
  with incus and put `Verified: ./smoke.sh` in the commit message. If you are
  in a sandbox (no incus), say "Not verified against real incus" instead and
  tell the user to run it.
- **Every change comes with tests when feasible.** New behaviour gets a test.
  A bug fix gets a test that fails without the fix. Changes to
  `sandbox_CLAUDE.md` are covered by `test_render_claude_md_real_template`,
  which fails on any unsubstituted placeholder.
- **Validate inline, and everything must pass before committing.**
  - Lint synchronously for all code you changed. Here the whole-repo lint
    (`pyflakes`, `mypy`, `shellcheck`) takes a few seconds, so just lint
    everything. Judge a run by its exit status, not the tail of its output:
    `pytest | tail` hides a failure, so use `set -o pipefail` or check
    PIPESTATUS.
  - Run the specific tests that cover your change. The whole unit suite is
    also fast here (about a second or less), so running all of it inline is
    fine. If it ever becomes slow enough to hurt interactivity, go back to
    targeted tests and leave the full picture to CI. The `incus` integration
    tests are opt-in and are covered by CI's `integration` job.
  - After pushing, start a background watcher on the CI run and fix failures
    as they come back (see "CI after every push" in `sandbox_CLAUDE.md`).
  - Don't commit with failing or skipped-because-broken tests, and don't
    weaken or delete a test just to make it pass.
- **Be pragmatic, not exhaustive.** Test the logic that can break: validation,
  security-relevant quoting and escaping, cache and freshness decisions,
  teardown guarantees, and command construction. Skip trivial glue and
  re-testing the standard library. A few focused tests beat many brittle ones.
- If something genuinely can't be tested automatically (it needs incus, a
  real GitHub key or an interactive TTY), extract the decision logic into a
  pure function and test that. Say in the commit what was verified manually.
