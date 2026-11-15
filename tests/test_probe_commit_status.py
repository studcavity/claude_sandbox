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
rd() { cat "$D/$1" 2>/dev/null || echo "$2"; }
val() { printf '%s' "$a" | sed -n "s/.* -f $1=\([^ ]*\).*/\1/p"; }
inc() { v=$(( $(rd "$1" 0) + 1 )); echo $v > "$D/$1"; echo $v; }
enforced() { # is the gate closed for the sha?
  [ "${FAKE_ENFORCE:-1}" = 1 ] && [ -f "$D/rs_exists" ] && [ "$(rd rs_enf active)" = active ] \
    && [ "$(rd rs_rules 1)" -ge 1 ]
}
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
  "api -X PUT repos/"*"/contents/"*)
    b=$(val branch); k=$(inc commits); echo "head$k" > "$D/branch-$b"
    echo "{\"commit\":{\"sha\":\"head$k\"},\"content\":{\"sha\":\"file$k\"}}"; exit 0 ;;
  "api -X POST repos/"*"/pulls"*) p=$(inc prs); echo "$(val head)" > "$D/pr-$p"; echo "$p"; exit 0 ;;  # PULLS-POST
  "api -X POST repos/"*"/rulesets"*)
    cat > /dev/null
    [ "${FAKE_NO_RULESET:-0}" = 1 ] && { echo "Upgrade to GitHub Pro" >&2; exit 1; }
    touch "$D/rs_exists"; echo 7; exit 0 ;;
  "api -X PUT repos/"*"/rulesets/7"*"--input -"*)
    cat > /dev/null; echo active > "$D/rs_enf"; echo 1 > "$D/rs_rules"; exit 0 ;;
  "api -X PUT repos/"*"/rulesets/7"*)
    e=$(val enforcement)
    if [ "$e" = evaluate ] && [ "${FAKE_NO_EVALUATE:-0}" = 1 ]; then
      echo "Evaluate mode requires GitHub Enterprise (HTTP 422)" >&2; exit 1; fi
    echo "$e" > "$D/rs_enf"
    [ "${FAKE_PUT_REPLACES:-0}" = 1 ] && echo 0 > "$D/rs_rules"
    exit 0 ;;
  *"rulesets/7 --jq .enforcement"*) rd rs_enf active; exit 0 ;;
  *"rulesets/7 --jq"*) rd rs_rules 1; exit 0 ;;
  "api -X POST repos/"*"/statuses/"*)
    sha="${a##*statuses/}"; sha="${sha%% *}"; echo "$(val state)" > "$D/status-$sha"; exit 0 ;;
  *"/statuses --jq"*) echo "tester (type: User)"; exit 0 ;;
  "pr view"*)
    set -- $a; b=$(cat "$D/pr-$3"); sha=$(cat "$D/branch-$b")
    if enforced; then [ "$(rd "status-$sha" none)" = success ] && echo CLEAN || echo BLOCKED
    else echo CLEAN; fi
    exit 0 ;;
  "api -X PUT repos/"*"/pulls/"*"/merge"*)
    n="${a#*pulls/}"; n="${n%%/*}"; b=$(cat "$D/pr-$n"); sha=$(cat "$D/branch-$b")
    if ! enforced || [ "$(rd "status-$sha" none)" = success ]; then echo '{"merged":true}'; exit 0; fi
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


def test_missing_tool_is_reported_before_anything_is_created(env):
    import shutil
    e, state = env
    only_gh = os.path.join(os.path.dirname(e["PROBE"]), "bin")   # fake gh, no jq
    e = dict(e, PATH=only_gh)
    r = subprocess.run([shutil.which("bash"), e["PROBE"], "--yes"], env=e,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "missing required tool: jq" in r.stderr
    assert not (state / "calls").exists()      # gh was never even invoked


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
                       "merge succeeds after success on the new head",
                       "level 1: override makes the PR mergeable",
                       "level 1: override does not survive a new commit",
                       "level 2: partial PUT kept the rules",
                       "level 2: merge succeeds while the ruleset is disabled",
                       "gate is closed again after re-enabling"):
            assert needle in r.stdout
        assert (state / "deleted").exists()

    def test_partial_put_that_replaces_the_ruleset_is_a_failure_with_fallback(self, env):
        e, state = env
        e["FAKE_PUT_REPLACES"] = "1"
        r, _ = run((e, state), "--yes")
        assert r.returncode == 1
        assert "FAIL  level 2: partial PUT kept the rules" in r.stdout
        assert "re-send the full ruleset" in r.stdout
        # the script restored the full ruleset, so the gate still closes again
        assert "PASS  gate is closed again after re-enabling" in r.stdout

    def test_evaluate_mode_refusal_is_informational(self, env):
        e, state = env
        e["FAKE_NO_EVALUATE"] = "1"
        r, _ = run((e, state), "--yes")
        assert r.returncode == 0, r.stdout
        assert "INFO  evaluate mode" in r.stdout and "Enterprise" in r.stdout

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
            'echo "$p"; exit 0 ;;  # PULLS-POST',
            'echo "$p"; exit 1 ;;  # PULLS-POST'))
        r, _ = run((e, state), "--yes")
        assert r.returncode != 0
        assert (state / "deleted").exists()


def test_shellcheck_clean():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    assert subprocess.run(["shellcheck", SCRIPT]).returncode == 0
