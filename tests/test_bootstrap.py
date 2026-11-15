"""Tests for bootstrap: venv creation, requirements stamp, re-exec."""
import os

import pytest

from claude_sandbox import bootstrap as sb
@pytest.fixture
def proj(tmp_path):
    (tmp_path / "requirements.txt").write_text("PyYAML>=6\n")
    return tmp_path


class Spy:
    def __init__(self, fail_on=None):
        self.runs, self.execs, self.fail_on = [], [], fail_on

    def run(self, cmd, check=True):
        self.runs.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            import subprocess
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[1:3] == ["-m", "venv"]:  # simulate venv creation
            os.makedirs(os.path.join(cmd[3], "bin"))
            open(os.path.join(cmd[3], "bin", "python"), "w").close()

    def execv(self, path, argv):
        self.execs.append((path, argv))


def ensure(proj, spy, **kw):
    sb.ensure_venv(str(proj), argv=["claude_sandbox.py", "proj"],
                   python="/usr/bin/python3", prefix="/usr", base_prefix="/usr",
                   run=spy.run, execv=spy.execv, environ=kw.pop("environ", {}),
                   **kw)


def test_first_run_creates_venv_installs_then_reexecs(proj):
    spy = Spy()
    ensure(proj, spy)
    venv_py = str(proj / "venv" / "bin" / "python")
    assert spy.runs[0] == ["/usr/bin/python3", "-m", "venv", str(proj / "venv")]
    assert spy.runs[1] == [venv_py, "-m", "pip", "install", "-q", "-r",
                           str(proj / "requirements.txt")]
    assert spy.execs == [(venv_py, [venv_py, "claude_sandbox.py", "proj"])]


def test_second_run_skips_pip_until_requirements_change(proj):
    ensure(proj, Spy())
    again = Spy()
    ensure(proj, again)
    assert again.runs == [] and len(again.execs) == 1  # no fork, just re-exec
    (proj / "requirements.txt").write_text("PyYAML>=6\ninvoke>=2\n")
    changed = Spy()
    ensure(proj, changed)
    assert len(changed.runs) == 1 and "pip" in changed.runs[0]


def test_failed_install_leaves_no_stamp_so_it_retries(proj):
    with pytest.raises(SystemExit, match="python3-venv"):
        ensure(proj, Spy(fail_on="pip" if False else "install"))
    assert not os.path.exists(proj / "venv" / sb.STAMP)
    retry = Spy()
    ensure(proj, retry)
    assert any("pip" in c for c in retry.runs)


@pytest.mark.parametrize("prefix, env", [
    ("/some/venv", {}),                       # already in a virtualenv
    ("/usr", {"CLAUDE_SANDBOX_NO_VENV": "1"}),  # explicit opt-out
])
def test_does_nothing_inside_a_venv_or_when_opted_out(proj, prefix, env):
    spy = Spy()
    sb.ensure_venv(str(proj), argv=["x"], prefix=prefix, base_prefix="/usr",
                   run=spy.run, execv=spy.execv, environ=env)
    assert spy.runs == [] and spy.execs == [] and not (proj / "venv").exists()
