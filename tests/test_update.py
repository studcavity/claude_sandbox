"""Tests for update against real throwaway git repos (no network)."""
import os
import subprocess

import pytest

from claude_sandbox import update as su
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, env=ENV,
                   capture_output=True)


def head(d):
    return subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout


@pytest.fixture
def clones(tmp_path):
    """(local, work): local is one commit behind its origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, env=ENV)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True,
                   env=ENV, capture_output=True)
    (work / "f").write_text("1")
    git(work, "add", "f")
    git(work, "commit", "-qm", "one")
    git(work, "push", "-q", "-u", "origin", "HEAD:main")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(origin), str(local)], check=True,
                   env=ENV, capture_output=True)
    (work / "f").write_text("2")
    git(work, "commit", "-qam", "two")
    git(work, "push", "-q")
    return local, work


class Spy:
    def __init__(self, answer=""):
        self.answer, self.prompts, self.execs = answer, [], []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answer

    def execv(self, path, argv):
        self.execs.append((path, argv))


def check(local, spy, interactive, environ=None):
    su.check_for_updates(str(local), ["claude_sandbox.py", "proj"],
                         interactive, spy.ask,
                         environ={} if environ is None else environ,
                         execv=spy.execv)


def test_behind_lists_commits(clones):
    local, _ = clones
    up, commits = su.behind(str(local))
    assert up == "origin/main" and len(commits) == 1 and "two" in commits[0]


def test_non_tty_notifies_but_never_prompts_or_pulls(clones, capsys):
    local, work = clones
    before, spy = head(local), Spy()
    check(local, spy, interactive=False)
    err = capsys.readouterr().err
    assert "1 commit(s) behind" in err and "git -C" in err
    assert spy.prompts == [] and spy.execs == [] and head(local) == before


def test_yes_pulls_then_reruns_same_argv_with_skip_flag(clones):
    local, work = clones
    spy, env = Spy("y"), {}
    check(local, spy, interactive=True, environ=env)
    assert head(local) == head(work)
    (path, argv), = spy.execs
    assert argv[1:] == ["claude_sandbox.py", "proj"]
    assert env[su.SKIP_ENV] == "1"  # the re-exec must not ask again


@pytest.mark.parametrize("answer", ["", "n", "no", "maybe"])
def test_anything_but_yes_does_not_pull(clones, answer):
    local, _ = clones
    before, spy = head(local), Spy(answer)
    check(local, spy, interactive=True)
    assert head(local) == before and spy.execs == []


def test_diverged_checkout_survives_failed_pull(clones, capsys):
    local, _ = clones
    (local / "g").write_text("x")
    git(local, "add", "g")
    git(local, "commit", "-qm", "local-only")
    before, spy = head(local), Spy("y")
    check(local, spy, interactive=True)
    assert "pull failed" in capsys.readouterr().err
    assert spy.execs == [] and head(local) == before  # no merge commit


def test_up_to_date_is_silent(clones, capsys):
    local, _ = clones
    git(local, "pull", "-q", "--ff-only")
    spy = Spy()
    check(local, spy, interactive=True)
    assert capsys.readouterr().err == "" and spy.prompts == []


def test_skip_env_disables_everything(clones, capsys):
    local, _ = clones
    spy = Spy("y")
    check(local, spy, interactive=True, environ={su.SKIP_ENV: "1"})
    assert capsys.readouterr().err == "" and spy.prompts == []


def test_not_a_git_checkout_or_no_upstream_is_quiet(tmp_path, capsys):
    spy = Spy()
    check(tmp_path, spy, interactive=True)
    git_dir = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(git_dir)], check=True)
    check(git_dir, spy, interactive=True)
    assert capsys.readouterr().err == "" and spy.prompts == []


def test_offline_fetch_failure_is_a_note_not_an_error(clones, capsys):
    local, _ = clones
    git(local, "remote", "set-url", "origin", "/nonexistent/repo.git")
    check(local, Spy(), interactive=True)
    assert "couldn't check for updates" in capsys.readouterr().err


def test_missing_git_binary_is_quiet(clones, monkeypatch):
    local, _ = clones

    def nogit(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(su.subprocess, "run", nogit)
    assert su.behind(str(local)) is None
