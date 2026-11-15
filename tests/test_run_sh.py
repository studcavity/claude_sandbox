"""Static checks for run.sh (full runs need incus + gh on a real host)."""
import os
import shutil
import subprocess

import pytest


def test_run_sh_syntax(repo_root):
    subprocess.run(["bash", "-n", os.path.join(repo_root, "run.sh")],
                   check=True)


def test_run_sh_usage_without_args(repo_root):
    r = subprocess.run(["bash", os.path.join(repo_root, "run.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "Usage" in r.stdout


@pytest.mark.skipif(not shutil.which("shellcheck"),
                    reason="shellcheck not installed")
def test_run_sh_shellcheck(repo_root):
    # Same inputs as CI: run.sh sources update_check.sh, which shellcheck only
    # follows when it is given as input too.
    subprocess.run(["shellcheck", os.path.join(repo_root, "run.sh"),
                    os.path.join(repo_root, "update_check.sh")], check=True)
