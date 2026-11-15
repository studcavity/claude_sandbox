import argparse
import datetime
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        base = dict(repo=None, read_only=False, image="ubuntu:24.04",
                    cli="incus", name="claude-test", git_name=None,
                    git_email=None, shell=False, tmux=False, no_cache=False,
                    rebuild=False, max_age=7, model="default", no_deps=False,
                    claude_md=os.path.join(REPO_ROOT, "sandbox_CLAUDE.md"),
                    setup_token=False,
                    token_file=str(tmp_path / "oauth_token"))
        base.update(kw)
        return argparse.Namespace(**base)
    return _make
