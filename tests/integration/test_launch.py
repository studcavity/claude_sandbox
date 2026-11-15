"""Smoke test: the launcher's own lifecycle in a REAL container.

Runs Sandbox.launch, provision_key, clone, write_claude_md, session and
teardown unmodified against real incus, for the GitHub backend and for
--local (whose bundle in, export and import then run for real too). Only GitHub is faked (`gh_api`), and
the clone is pointed at a local bare repo inside the container with a git
`insteadOf` rule, so the real `git clone git@github.com:o/r.git` path runs
without network access to GitHub or a real deploy key.

This is the layer unit tests can't see: bugs *between* the pieces (an
attribute shadowing a method, a probe script that only fails in a real shell, a
secrets file that isn't removed, a session started in the wrong directory).

Opt-in like the other integration tests: CS_INTEGRATION=1 plus incus.
Needs an Ubuntu image (the session user, bash -l, apt); override with
CS_SMOKE_IMAGE. Skipped volumes/pool/image-cache steps have their own tests.
"""
import os
import shutil
import subprocess
import uuid

import pytest

from claude_sandbox import cli as cs
from claude_sandbox.backends import github as bg
pytestmark = [pytest.mark.incus, pytest.mark.smoke]

CLI = next((c for c in ("incus", "lxc") if shutil.which(c)), None)
if os.environ.get("CS_INTEGRATION") != "1" or CLI is None:
    pytest.skip("set CS_INTEGRATION=1 and install incus to run",
                allow_module_level=True)

IMAGE = os.environ.get("CS_SMOKE_IMAGE", "images:ubuntu/24.04")
ORIGIN = "/home/ubuntu/origin.git"
FAKE_TOKEN = "sk-ant-smoke-test-not-a-secret"

# Fed to the session's `bash -l` on stdin: report what the session sees, make
# a commit on the session branch and push it.
SESSION_PROBE = """\
echo "pwd=$(pwd)"
echo "branch=$(git rev-parse --abbrev-ref HEAD)"
echo "token=${CLAUDE_CODE_OAUTH_TOKEN:-unset}"
echo "secrets_file=$(test -e ~/.sandbox_secrets && echo present || echo gone)"
echo smoke >> smoke.txt && git add smoke.txt && git commit -qm "smoke commit"
git push -q && echo "pushed=yes" || echo "pushed=no"
exit
"""


def kv(text):
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


@pytest.fixture
def box(tmp_path, monkeypatch):
    name = f"cs-smoke-{uuid.uuid4().hex[:6]}"
    args = cs.build_parser().parse_args([
        "o/r", "--name", name, "--shell", "--cli", CLI, "--no-update-check",
        "--no-usb", "--no-deps", "--disk-size", "0",  # dir pools have no quota
        "--token-file", str(tmp_path / "tok"),
        "--usb-allow", str(tmp_path / "allow")])
    cs.normalize_args(args)

    calls = []

    def fake_gh(method, path, body=None, raw=False):
        calls.append((method, path))
        if path == "/meta":
            return {"ssh_keys": []}
        if method == "POST" and path.endswith("/keys"):
            return {"id": 1}
        return None  # DELETE
    monkeypatch.setattr(bg, "gh_api", fake_gh)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)

    sb = cs.Sandbox(args)
    sb.gh_calls = calls
    try:
        yield sb
    finally:
        sb.teardown()


def seed_origin(sb):
    """A bare repo in the container that stands in for github.com:o/r."""
    sb.box.user(
        "git config --global user.name smoke && "
        "git config --global user.email smoke@example.invalid && "
        f"git init -q --bare -b main {ORIGIN} && "
        "git clone -q " + ORIGIN + " /tmp/seed && cd /tmp/seed && "
        "echo hello > README && git add README && "
        "git commit -qm init && git push -q origin main && cd / && "
        f'git config --global url."{ORIGIN}".insteadOf git@github.com:o/r.git')


def test_full_lifecycle_in_a_real_container(box, monkeypatch):
    box.launch(IMAGE)
    seed_origin(box)
    box.install_deps()
    box.provision_key()
    workdir = box.clone()
    box.run_setup(workdir)
    box.write_claude_md(workdir)

    # the real clone landed on the session's own branch, not main
    branch = f"sbx/{box.name}/work"
    out = box.box.user(f"git -C {workdir} rev-parse --abbrev-ref HEAD",
                       capture=True).stdout.strip()
    assert out == branch

    # the real probe and template rendered, with no leftover placeholders
    md = box.box.user("cat ~/.claude/CLAUDE.md", capture=True).stdout
    assert box.name in md and f"sbx/{box.name}/" in md
    assert "$session" not in md and "$prefix" not in md and "$repo" not in md

    # the sandbox marker exists and names this session
    marker = box.box.root_argv(["cat", cs.SANDBOX_MARKER]).stdout.decode()
    assert marker.strip() == box.name

    # run the real session() with a scripted stdin instead of a terminal; it
    # is the only call made without keyword arguments
    real_run, seen = subprocess.run, {}

    def run(cmd, **kw):
        if kw:
            return real_run(cmd, **kw)
        seen["p"] = real_run(cmd, input=SESSION_PROBE, text=True,
                             capture_output=True)
        return seen["p"]
    monkeypatch.setattr(cs.subprocess, "run", run)
    box.session(workdir)
    p = seen["p"]
    assert p.returncode == 0, p.stderr
    facts = kv(p.stdout)
    assert facts["pwd"] == workdir
    assert facts["branch"] == branch
    assert facts["token"] == FAKE_TOKEN        # the credential reached the session...
    assert facts["secrets_file"] == "gone"     # ...and its file was removed
    assert facts["pushed"] == "yes", p.stderr

    # the push landed on the session branch of the (local) origin only
    heads = box.box.user(f"git --git-dir={ORIGIN} branch --format='%(refname:short)'",
                         capture=True).stdout.split()
    assert sorted(heads) == sorted(["main", branch])

    box.teardown()
    assert ("DELETE", "/repos/o/r/keys/1") in box.gh_calls
    gone = subprocess.run([CLI, "info", box.name], capture_output=True)
    assert gone.returncode != 0, "container should have been destroyed"


# --------------------------------------------------------------------------- #
# --local: no GitHub at all; the base goes in and the work comes out as bundles
# --------------------------------------------------------------------------- #
LOCAL_PROBE = """\
echo "branch=$(git rev-parse --abbrev-ref HEAD)"
echo "base=$(git rev-parse origin/HEAD)"
echo "key=$(test -e ~/.ssh/id_ed25519 && echo present || echo none)"
echo smoke >> smoke.txt && git add smoke.txt && git commit -qm "smoke commit" && echo committed=yes
git branch -f main HEAD    # a session moving its own main must not reach the host
exit
"""


def test_local_lifecycle_in_a_real_container(tmp_path, monkeypatch, capfd):
    for k, v in dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e.invalid",
                     GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e.invalid").items():
        monkeypatch.setenv(k, v)
    primary = tmp_path / "proj"
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    (primary / "README").write_text("hello\n")
    subprocess.run(["git", "-C", str(primary), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(primary), "commit", "-qm", "init"], check=True)
    main_before = subprocess.run(["git", "-C", str(primary), "rev-parse", "main"],
                                 check=True, capture_output=True, text=True).stdout

    name = f"cs-smoke-{uuid.uuid4().hex[:6]}"
    args = cs.build_parser().parse_args([
        "--local", str(primary), "--name", name, "--shell", "--cli", CLI,
        "--no-update-check", "--no-usb", "--no-deps", "--disk-size", "0",
        "--token-file", str(tmp_path / "tok"), "--usb-allow", str(tmp_path / "allow")])
    cs.normalize_args(args)
    sb = cs.Sandbox(args)
    try:
        assert sb.backend.check_host() is None
        sb.launch(IMAGE)
        sb.install_deps()
        sb.provision_key()
        workdir = sb.clone()
        sb.write_claude_md(workdir)
        md = sb.box.user("cat ~/.claude/CLAUDE.md", capture=True).stdout
        assert "<!--" not in md and "deploy key" not in md

        real_run, seen = subprocess.run, {}

        def run(cmd, **kw):
            if kw:
                return real_run(cmd, **kw)
            seen["p"] = real_run(cmd, input=LOCAL_PROBE, text=True,
                                 capture_output=True)
            return seen["p"]
        monkeypatch.setattr(cs.subprocess, "run", run)
        sb.session(workdir)
        monkeypatch.setattr(cs.subprocess, "run", real_run)
        facts = kv(seen["p"].stdout)
        assert facts["branch"] == f"sbx/{name}/work"
        assert facts["base"] == main_before.strip()
        assert facts["key"] == "none"            # no network credential at all
        assert facts["committed"] == "yes", seen["p"].stderr

        sb.export()
        assert sb.backend.exported, "export produced no bundle"
    finally:
        sb.teardown()
    sb.land()

    git = lambda *a: real_run(["git", "-C", str(primary), *a], check=True,  # noqa: E731
                              capture_output=True, text=True).stdout
    assert git("rev-parse", "main") == main_before          # untouched
    assert git("show", f"sbx/{name}/work:smoke.txt") == "smoke\n"
    err = capfd.readouterr().err
    assert f"branches imported from session {name}" in err
    assert "signed by session" in err                       # provenance checked
    gone = subprocess.run([CLI, "info", name], capture_output=True)
    assert gone.returncode != 0, "container should have been destroyed"
