import argparse
import datetime
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def no_real_container_runtime(monkeypatch):
    """Tests never ask a real incus/lxc what containers exist."""
    import sandbox_containers
    monkeypatch.setattr(sandbox_containers.Container, "list_names",
                        staticmethod(lambda cli: set()))


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

    def add_usb(self, vid, pid):
        self.calls.append(("add_usb", vid, pid))

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
                    no_update_check=True, read_only=False, image=None,
                    cli="incus", name="claude-test", git_name=None,
                    git_email=None, shell=False, tmux=False, no_cache=False,
                    rebuild=False, nesting=False, max_age=7, model="default", no_deps=False,
                    usb=[], no_usb=False,
                    usb_allow=str(tmp_path / "usb_allow"),
                    claude_md=os.path.join(REPO_ROOT, "sandbox_CLAUDE.md"),
                    setup_token=False, disk_size=None, volume_size=None,
                    token_file=str(tmp_path / "oauth_token"))
        base.update(kw)
        return argparse.Namespace(**base)
    return _make


@pytest.fixture(autouse=True)
def no_real_pool_check(monkeypatch, request):
    """main() checks the incus pool; never let unit tests reach a real one."""
    if request.module.__name__ != "test_pool":
        import sandbox_pool
        monkeypatch.setattr(sandbox_pool, "check", lambda cli, grow_to=None: None)
