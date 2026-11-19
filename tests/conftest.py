import argparse
import datetime
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def no_real_container_runtime(monkeypatch):
    """Tests never ask a real incus/lxc what containers exist."""
    from claude_sandbox.container import incus as incus
    monkeypatch.setattr(incus.Container, "list_names",
                        staticmethod(lambda cli: set()))


@pytest.fixture(autouse=True)
def private_state_dir(monkeypatch, tmp_path):
    """Recorded signing keys go to a per-test directory, never ~/.local."""
    from claude_sandbox import provenance as provenance
    monkeypatch.setenv(provenance.STATE_ENV, str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def no_real_provenance(monkeypatch, request):
    """Tests never post provenance statuses to a real GitHub (its own tests
    pass fakes instead)."""
    from claude_sandbox import provenance as provenance
    if request.module.__name__.endswith("test_provenance"):
        return
    monkeypatch.setattr(provenance, "post_session", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def no_real_skill_update(monkeypatch, request):
    """Tests never run `git submodule update` (network); test_skills.py
    exercises refresh itself with a fake invoke Context."""
    from claude_sandbox import skills as skills
    if request.module.__name__.endswith("test_skills"):
        return
    monkeypatch.setattr(skills, "refresh", lambda *a, **k: (True, ""))


@pytest.fixture
def repo_root():
    return REPO_ROOT


@pytest.fixture
def now():
    return datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc)


class FakeBox:
    """Records calls made to a Container instead of running them."""

    def __init__(self):
        self.calls = []

    def root_argv(self, argv, env=None, cwd=None, input=None, capture=True):
        self.calls.append(("root_argv", list(argv), env, cwd))

    def put_file(self, path, data, mode="0644"):
        self.calls.append(("put_file", path, data, mode))

    def put_user_file(self, rel, data, mode="0644"):
        self.calls.append(("put_user_file", rel, data, mode))

    def add_usb(self, vid, pid):
        self.calls.append(("add_usb", vid, pid))

    def add_mount(self, index, source, path, readonly=True):
        self.calls.append(("add_mount", index, source, path, readonly))

    def self_check(self):
        self.calls.append(("self_check",))

    def root(self, script, **kw):
        self.calls.append(("root", script))

    def user(self, script, **kw):
        self.calls.append(("user", script))


@pytest.fixture
def fake_box():
    return FakeBox()


@pytest.fixture
def make_args(tmp_path):
    def _make(**kw):
        base = dict(repo=None, repository=None, account_key=False,
                    github_user="studcavity", no_user_check=False,
                    no_update_check=True, read_only=False, no_pr=False, no_protect_prompt=False, no_hardening_prompt=False, no_provenance=False, timings=False, timings_json=None, no_prefetch=False, hard_max_age=30, refresh_images=False,
                    land_all=False, image=None, local=None, base=None, recover=False,
                    cli="incus", name="claude-test", git_name=None,
                    git_email=None, shell=False, tmux=False, no_cache=False,
                    rebuild=False, nesting=False, max_age=7, model="default", no_deps=False, no_final_check=False,
                    usb=[], no_usb=False, mount=[], allow_mounts=False, no_mounts=False,
                    allow_files=False, no_files=False, allow_nesting=False,
                    usb_allow=str(tmp_path / "usb_allow"),
                    claude_md=os.path.join(REPO_ROOT, "claude_sandbox", "data", "sandbox_CLAUDE.md"),
                    setup_token=False, disk_size=None, volume_size=None,
                    token_file=str(tmp_path / "oauth_token"))
        base.update(kw)
        return argparse.Namespace(**base)
    return _make


@pytest.fixture(autouse=True)
def no_real_pool_check(monkeypatch, request):
    """main() checks the incus pool; never let unit tests reach a real one."""
    if request.module.__name__ != "test_pool":
        from claude_sandbox.container import pool as pool
        monkeypatch.setattr(pool, "check", lambda cli, grow_to=None: None)


@pytest.fixture(autouse=True)
def no_real_prefetch(monkeypatch, request):
    """The launch starts GitHub reads in threads through the real gh wrapper;
    tests fake `gh_api` per module, so keep the prefetch from reaching a real
    `gh` (test_prefetch turns it back on against a faked `_gh`)."""
    if request.module.__name__ != "test_prefetch":
        from claude_sandbox.github import gh as gh
        monkeypatch.setattr(gh, "PREFETCH_ENABLED", False)
