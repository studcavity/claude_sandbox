"""scripts/probe_commit_status.sh against a fake `gh` (no network, no GitHub).

The fake models just enough GitHub: a ruleset that requires the status on the
PR's current head (or, with FAKE_ENFORCE=0, one that enforces nothing).
"""
import os
import shutil
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                      "probe_commit_status.sh")
GUARD = '[ -f /etc/claude-sandbox ]'

FAKE_GH = r'''#!/usr/bin/env bash
D="$FAKE_DIR"; a="$*"; echo "$a" >> "$D/calls"
n() { cat "$D/n" 2>/dev/null || echo 0; }
case "$a" in
  "auth status") exit 0 ;;
  "--version") echo "gh version 0.0-fake"; exit 0 ;;
  "api -i /") printf 'HTTP/2 200\r\nX-OAuth-Scopes: repo, delete_repo\r\n\r\n{}'; exit 0 ;;
  "api user --jq .login") echo tester; exit 0 ;;
  "repo create"*) exit 0 ;;
  "repo delete"*) echo deleted > "$D/deleted"; exit 0 ;;
  *"--jq .default_branch") echo main; exit 0 ;;
  *"git/ref/heads/main"*) echo base0; exit 0 ;;
  "api -X POST repos/"*"/git/refs"*) exit 0 ;;
  "api -X PUT repos/"*"/contents/probe.txt"*)
    k=$(( $(n) + 1 )); echo $k > "$D/n"; echo "head$k" > "$D/head"
    echo "{\"commit\":{\"sha\":\"head$k\"},\"content\":{\"sha\":\"file$k\"}}"; exit 0 ;;
  "api -X POST repos/"*"/pulls"*) echo 1; exit 0 ;;
  "api -X POST repos/"*"/rulesets"*) cat > /dev/null; [ "${FAKE_NO_RULESET:-0}" = 1 ] && { echo "Upgrade to GitHub Pro" >&2; exit 1; }; exit 0 ;;
  "api -X POST repos/"*"/statuses/"*)
    sha="${a##*statuses/}"; sha="${sha%% *}"
    st=$(printf '%s' "$a" | sed -n 's/.*-f state=\([a-z]*\).*/\1/p')
    echo "$st" > "$D/status-$sha"; exit 0 ;;
  *"/statuses --jq"*) echo "tester (type: User)"; exit 0 ;;
  "pr view"*)
    if [ "${FAKE_ENFORCE:-1}" = 0 ]; then echo CLEAN; exit 0; fi
    h=$(cat "$D/head"); [ "$(cat "$D/status-$h" 2>/dev/null)" = success ] && echo CLEAN || echo BLOCKED
    exit 0 ;;
  "api -X PUT repos/"*"/merge"*)
    h=$(cat "$D/head")
    if [ "${FAKE_ENFORCE:-1}" = 0 ] || [ "$(cat "$D/status-$h" 2>/dev/null)" = success ]; then
      echo '{"merged":true}'; exit 0; fi
    echo 'Required status check "sandbox/check" is expected. (HTTP 405)' >&2; exit 1 ;;
esac
echo "fake gh: unhandled: $a" >&2; exit 99
'''


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (tmp_path / "state").mkdir()
    # The real script refuses inside a sandbox (this suite may run in one), so
    # logic tests run a copy without that single guard line. The guard itself
    # is covered by test_guard_present_and_refuses_in_a_sandbox on the original.
    lines = open(SCRIPT).read().splitlines(keepends=True)
    kept = [l for l in lines if not l.startswith(GUARD)]
    assert len(kept) == len(lines) - 1
    copy = tmp_path / "probe.sh"
    copy.write_text("".join(kept))
    e = dict(os.environ, PROBE=str(copy), PATH=f"{bin_dir}:{os.environ['PATH']}",
             FAKE_DIR=str(tmp_path / "state"), POLL_TRIES="2", POLL_SLEEP="0")
    return e, tmp_path / "state"


def run(env, *args):
    e, state = env
    r = subprocess.run(["bash", e["PROBE"], *args], env=e, capture_output=True,
                       text=True, timeout=60)
    return r, state


def test_guard_present_and_refuses_in_a_sandbox():
    assert any(l.startswith(GUARD) for l in open(SCRIPT))
    if not os.path.exists("/etc/claude-sandbox"):
        pytest.skip("not inside a sandbox: can't observe the refusal")
    r = subprocess.run(["bash", SCRIPT, "--yes"], capture_output=True,
                       text=True)
    assert r.returncode == 1 and "refusing" in r.stderr


def test_requires_explicit_yes(env):
    r, state = run(env)
    assert r.returncode == 2 and "usage" in r.stderr
    assert not (state / "calls").exists()  # touched nothing


def test_rejects_bad_visibility(env):
    r, _ = run(env, "--yes", "--visibility", "internal")
    assert r.returncode == 2


class TestAgainstFakeGh:
    def test_enforcing_github_passes_and_repo_is_deleted(self, env):
        r, state = run(env, "--yes")
        assert r.returncode == 0, r.stdout + r.stderr
        for needle in ("ALL CHECKS PASSED",
                       "PR blocked without the status",
                       "new commit loses the status",
                       "merge succeeds after success on the new head"):
            assert needle in r.stdout
        assert (state / "deleted").exists()

    def test_unenforced_ruleset_is_reported_as_failure(self, env):
        e, state = env
        e["FAKE_ENFORCE"] = "0"
        r, _ = run((e, state), "--yes")
        assert r.returncode == 1
        assert "SOME CHECKS FAILED" in r.stdout
        assert "it MERGED" in r.stdout
        assert (state / "deleted").exists()  # still cleaned up

    def test_ruleset_refused_skips_blocking_steps(self, env):
        e, state = env
        e["FAKE_NO_RULESET"] = "1"
        r, _ = run((e, state), "--yes")
        assert r.returncode == 1
        assert "SKIP" in r.stdout and "Upgrade to GitHub Pro" in r.stdout

    def test_keep_flag_skips_deletion(self, env):
        r, state = run(env, "--yes", "--keep")
        assert r.returncode == 0 and not (state / "deleted").exists()
        assert "kept: https://github.com/tester/sandbox-status-probe-" in r.stdout

    def test_cleanup_runs_even_if_a_step_dies(self, env):
        e, state = env
        e["FAKE_DIR"] = str(state)
        # break the PR creation call: script aborts under set -e
        broken = state.parent / "bin" / "gh"
        broken.write_text(broken.read_text().replace(
            '"api -X POST repos/"*"/pulls"*) echo 1; exit 0 ;;',
            '"api -X POST repos/"*"/pulls"*) exit 1 ;;'))
        r, _ = run((e, state), "--yes")
        assert r.returncode != 0
        assert (state / "deleted").exists()


def test_shellcheck_clean():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    assert subprocess.run(["shellcheck", SCRIPT]).returncode == 0
