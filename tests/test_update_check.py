"""Tests for the launcher's self-update check (update_check.sh, run.sh)."""
import os
import pty
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
ENV.pop("RUN_SH_SKIP_UPDATE_CHECK", None)


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, env=ENV,
                   capture_output=True)


@pytest.fixture
def clones(tmp_path):
    """(local, origin_work): local is one commit behind origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, env=ENV)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   env=ENV, capture_output=True)
    (work / "f").write_text("1")
    git(work, "add", "f"); git(work, "commit", "-qm", "one")
    git(work, "push", "-q", "-u", "origin", "HEAD:main")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True,
                   env=ENV, capture_output=True)
    (work / "f").write_text("2")
    git(work, "commit", "-qam", "two"); git(work, "push", "-q")
    return local, work


# Prints DONE if the function returns (i.e. did not exec the re-run command).
SNIPPET = ('source "$1/update_check.sh"; '
           'check_for_updates "$2" echo RERUN; echo DONE')


def run_check(local, answer=None, tty=False):
    cmd = ["bash", "-c", SNIPPET, "x", ROOT, str(local)]
    if not tty:
        p = subprocess.run(cmd, capture_output=True, text=True, env=ENV,
                           stdin=subprocess.DEVNULL)
        return p.stdout + p.stderr
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("bash", cmd, ENV)
    out = b""
    os.write(fd, (answer + "\n").encode())
    try:
        while chunk := os.read(fd, 4096):
            out += chunk
    except OSError:
        pass
    os.waitpid(pid, 0)
    return out.decode()


def head(d):
    return subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout


def test_non_tty_notifies_but_never_pulls(clones):
    local, work = clones
    before = head(local)
    out = run_check(local)
    assert "1 commit(s) behind" in out and "DONE" in out
    assert head(local) == before


def test_tty_yes_pulls_then_reruns(clones):
    local, work = clones
    out = run_check(local, "y", tty=True)
    assert "RERUN" in out and "DONE" not in out
    assert head(local) == head(work)


def test_tty_default_no_does_not_pull(clones):
    local, work = clones
    before = head(local)
    out = run_check(local, "", tty=True)
    assert "DONE" in out and head(local) == before


def test_diverged_checkout_survives_failed_pull(clones):
    local, work = clones
    (local / "g").write_text("x")
    git(local, "add", "g"); git(local, "commit", "-qm", "local-only")
    before = head(local)
    out = run_check(local, "y", tty=True)
    assert "pull failed" in out and "DONE" in out and "RERUN" not in out
    assert head(local) == before  # ff-only: no merge commit created


def test_up_to_date_is_silent(clones):
    local, work = clones
    git(local, "pull", "-q", "--ff-only")
    assert run_check(local).strip() == "DONE"


def test_skip_env_disables_check(clones):
    local, work = clones
    cmd = ["bash", "-c", SNIPPET, "x", ROOT, str(local)]
    p = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**ENV, "RUN_SH_SKIP_UPDATE_CHECK": "1"})
    assert p.stdout.strip() == "DONE" and "behind" not in p.stderr


@pytest.fixture
def shims(tmp_path):
    """git/gh stubs: git logs its calls; gh reports the wrong user so run.sh
    exits right after the update check, before touching venv or incus."""
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "git.log"
    (b / "git").write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 1\n')
    (b / "gh").write_text("#!/bin/sh\necho someone-else\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return b, log


def run_sh(shims, *args):
    b, log = shims
    p = subprocess.run(["bash", os.path.join(ROOT, "run.sh"), *args],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={**ENV, "PATH": f"{b}:{ENV['PATH']}"})
    return p, (log.read_text() if log.exists() else "")


def test_run_sh_checks_by_default(shims):
    p, gitlog = run_sh(shims, "repo")
    assert gitlog and "unexpected user" in p.stdout


def test_run_sh_flag_skips_check_and_is_not_forwarded(shims):
    for args in (("repo", "--no-update-check"), ("--no-update-check", "repo")):
        p, gitlog = run_sh(shims, *args)
        assert gitlog == "" and "unexpected user" in p.stdout


def test_run_sh_flag_alone_prints_usage(shims):
    p, _ = run_sh(shims, "--no-update-check")
    assert p.returncode == 1 and "Usage" in p.stdout
