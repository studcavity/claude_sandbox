"""run.sh is only a shim: it must exec the program and nothing else."""
import os
import re
import shutil
import stat
import subprocess

import pytest


@pytest.fixture
def shim(tmp_path, repo_root):
    """run.sh with a fake python3 first on PATH that prints its argv."""
    fake = tmp_path / "bin" / "python3"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}"}

    def run(*args, cwd=None, script=None):
        p = subprocess.run(["bash", script or os.path.join(repo_root, "run.sh"),
                            *args], capture_output=True, text=True, env=env,
                           cwd=cwd, check=True)
        return p.stdout.splitlines()
    return run


def test_run_sh_syntax(repo_root):
    subprocess.run(["bash", "-n", os.path.join(repo_root, "run.sh")], check=True)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="no shellcheck")
def test_run_sh_shellcheck(repo_root):
    subprocess.run(["shellcheck", os.path.join(repo_root, "run.sh")], check=True)


def test_passes_every_argument_through_untouched(shim, repo_root):
    out = shim("repo", "--tmux", "a b", "--model=x;y", "--no-update-check")
    assert out[0] == os.path.join(repo_root, "claude_sandbox.py")
    assert out[1:] == ["repo", "--tmux", "a b", "--model=x;y",
                       "--no-update-check"]


def test_works_from_any_directory_and_by_bare_name(shim, repo_root, tmp_path):
    assert shim("--setup-token", cwd=tmp_path)[1:] == ["--setup-token"]
    here = shim("x", cwd=repo_root, script="run.sh")  # BASH_SOURCE has no slash
    assert here[0].endswith("claude_sandbox.py") and here[1:] == ["x"]
    assert os.path.isfile(os.path.join(repo_root, here[0]))


def test_stays_a_thin_shim(repo_root):
    """No subshells, pipes or external commands: the only process started is
    the exec'd python. Logic belongs in claude_sandbox.py."""
    code = [l for l in open(os.path.join(repo_root, "run.sh")).read().splitlines()
            if l.strip() and not l.lstrip().startswith("#")]
    assert len(code) <= 5
    body = "\n".join(code)
    assert not re.search(r"\$\(|`|\||\bgit\b|\bgh\b|\bpip\b|\bvenv\b", body)
    assert "exec python3" in body
