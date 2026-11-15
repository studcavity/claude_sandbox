"""Guards on the repo's own safety nets (type checking, strict shell, CI).

These exist because each safety net can be weakened silently: drop one flag
and everything stays green while a whole class of bug stops being caught.
"""
import glob
import os
import re
import subprocess
import sys
import tomllib

import pytest
import yaml

STRICT = "set -euo pipefail"


def shell_scripts(root):
    found = [os.path.join(root, p) for p in ("run.sh", "scripts/smoke.sh")]
    found += sorted(glob.glob(os.path.join(root, ".githooks", "*")))
    found += sorted(glob.glob(os.path.join(root, "scripts", "*.sh")))
    return sorted(set(found))


def test_every_shell_script_runs_in_strict_mode(repo_root):
    """`set -euo pipefail` near the top, so a failing stage of `a | b` can't
    be hidden by `b` succeeding (the `pytest | tail` trap)."""
    scripts = shell_scripts(repo_root)
    assert len(scripts) >= 3
    for path in scripts:
        head = open(path).read().splitlines()[:25]
        assert any(l.strip() == STRICT for l in head), f"{path}: no '{STRICT}'"


def test_run_sh_still_works_with_no_arguments(repo_root, tmp_path):
    """-u must not break an empty "$@" in the shim."""
    fake = tmp_path / "python3"
    fake.write_text('#!/bin/sh\necho "args=$#"\n')
    fake.chmod(0o755)
    p = subprocess.run(["bash", os.path.join(repo_root, "run.sh")],
                       capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
    assert p.returncode == 0 and p.stdout.strip() == "args=1"  # script path only


# -- the pre-push hook keeps working under pipefail ------------------------- #
@pytest.fixture
def hook_repo(tmp_path, repo_root):
    def git(*a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                               *a], cwd=tmp_path, check=True, text=True,
                              capture_output=True).stdout.strip()
    git("init", "-q")
    (tmp_path / "README").write_text("x")
    git("add", "."), git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")

    def commit_touching(path, message):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("changed")
        git("add", "."), git("commit", "-qm", message)
        return git("rev-parse", "HEAD")

    def push(local, remote):
        p = subprocess.run(
            ["bash", os.path.join(repo_root, ".githooks", "pre-push")],
            input=f"refs/heads/x {local} refs/heads/x {remote}\n",
            cwd=tmp_path, capture_output=True, text=True)
        return p.returncode, p.stderr
    return base, commit_touching, push


def test_hook_warns_on_unverified_container_code(hook_repo):
    base, commit_touching, push = hook_repo
    rc, err = push(commit_touching("claude_sandbox/container/images.py", "tweak images"), base)
    assert rc == 0 and "WARNING" in err and "smoke.sh" in err


def test_hook_silent_when_verified_or_unrelated(hook_repo):
    base, commit_touching, push = hook_repo
    rc, err = push(commit_touching("claude_sandbox/container/deps.py",
                                   "x\n\nVerified: ./smoke.sh"), base)
    assert (rc, err) == (0, "")
    head = commit_touching("docs.md", "docs only")
    mid = head  # nothing watched between mid and head
    rc, err = push(commit_touching("docs2.md", "more docs"), mid)
    assert (rc, err) == (0, "")


def test_hook_never_blocks_a_push_even_on_garbage(hook_repo):
    _, _, push = hook_repo
    assert push("0" * 40, "0" * 40)[0] == 0           # branch delete
    assert push("deadbeef" * 5, "cafebabe" * 5)[0] == 0  # unknown shas


# -- mypy: the setting that makes it catch the shadowing bug ---------------- #
def test_mypy_config_checks_untyped_defs(repo_root):
    with open(os.path.join(repo_root, "pyproject.toml"), "rb") as f:
        cfg = tomllib.load(f)
    assert cfg["tool"]["mypy"]["check_untyped_defs"] is True


def test_mypy_catches_an_attribute_shadowing_a_method(repo_root, tmp_path):
    """The exact bug that crashed every launch: `self.session = ...` hid the
    session() method. With the repo's pyproject.toml, mypy must flag it."""
    src = tmp_path / "shadow.py"
    src.write_text("class Sandbox:\n"
                   "    def __init__(self, name):\n"
                   "        self.session = name\n"
                   "    def session(self, workdir):\n"
                   "        return workdir\n")
    p = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file",
         os.path.join(repo_root, "pyproject.toml"), "--cache-dir", os.devnull,
         str(src)], capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode != 0
    assert "method-assign" in p.stdout or "Cannot assign to a method" in p.stdout


def test_mypy_passes_on_the_repo(repo_root):
    p = subprocess.run(
        [sys.executable, "-m", "mypy", "--cache-dir", os.devnull,
         "claude_sandbox", *sorted(glob.glob(
             os.path.join(repo_root, "scripts", "*.py")))],
        capture_output=True, text=True, cwd=repo_root)
    assert p.returncode == 0, p.stdout


# -- CI workflow ------------------------------------------------------------ #
@pytest.fixture
def workflow(repo_root):
    with open(os.path.join(repo_root, ".github", "workflows", "tests.yml")) as f:
        return yaml.safe_load(f)


def test_ci_default_shell_is_strict(workflow):
    """GitHub's default is `bash -e {0}` (no pipefail), which would let
    `pytest | tee out` pass while pytest fails."""
    shell = workflow["defaults"]["run"]["shell"]
    flags = "".join(re.findall(r"\s-([a-z]+)", shell))  # "-euo" -> "euo"
    assert "e" in flags and "u" in flags and "pipefail" in shell


def test_ci_runs_mypy_and_the_smoke_step(workflow):
    steps = [s for j in workflow["jobs"].values() for s in j["steps"]]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "mypy" in runs
    assert "pytest -m smoke tests/integration/test_launch.py" in runs
    # no step overrides the strict default
    assert not [s for s in steps if "shell" in s]


def test_dev_requirements_include_mypy(repo_root):
    reqs = open(os.path.join(repo_root, "requirements-dev.txt")).read()
    assert "mypy" in reqs


# -- package layout (spec 09) ----------------------------------------------- #
def test_package_init_files_stay_empty(repo_root):
    """claude_sandbox/__init__.py is copied into the sandbox beside portable/
    and must not import anything; the subpackage ones are empty too."""
    for sub in ("", "backends", "container", "github", "portable"):
        init = os.path.join(repo_root, "claude_sandbox", sub, "__init__.py")
        assert os.path.getsize(init) == 0, init


def test_nothing_but_the_package_lives_at_the_root_as_python(repo_root):
    """The root is for entry shims, docs and config, not modules."""
    assert glob.glob(os.path.join(repo_root, "*.py")) == []
