"""Tests for sandbox_check against a real throwaway git repository (local
only: no network, no GitHub)."""
import os
import subprocess

import pytest

import sandbox_check as sc

S = "quirky-hopper"
ID = f"smoke-20261115-{S}-a3f9"
ACTION = ('Sandbox-Action: {"v": 1, "type": "issue", "id": "%s", '
          '"title": "t"}' % ID)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def git(*a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", *a], cwd=tmp_path,
                       check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)

    def add(message, *paths):
        for p in paths:
            f = tmp_path / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        (tmp_path / "n.txt").write_text(message)
        git("add", "-A")
        git("commit", "-q", "-m", message)
    return add


def trailer_block(*lines):
    return "feat: x\n\nbody\n\n" + "\n".join(lines)


def test_clean_range_passes(repo):
    repo(trailer_block(ACTION), f"todo/{ID}.md")
    assert sc.check_range("main~1", "main", f"sbx/{S}/work") == []
    assert sc.main(["main~1", "main", "--branch", f"sbx/{S}/work"]) == 0


def test_missing_todo_file_is_reported_with_the_sha(repo, capsys):
    repo(trailer_block(ACTION), "other.md")
    ((sha, problem),) = sc.check_range("main~1", "main", f"sbx/{S}/work")
    assert len(sha) == 7 and f"todo/{ID}.md" in problem
    assert sc.main(["main~1", "main"]) == 1
    assert "::error::" in capsys.readouterr().out


def test_deleting_the_todo_file_does_not_satisfy_the_rule(repo, tmp_path):
    repo("seed", f"todo/{ID}.md")
    (tmp_path / "todo" / f"{ID}.md").unlink()
    repo(trailer_block(ACTION))  # n.txt only
    assert sc.check_range("main~1", "main", f"sbx/{S}/work")


def test_session_branch_commits_need_no_trailer(repo):
    repo("plain commit")
    assert sc.check_range("main~1", "main", f"sbx/{S}/work") == []
    assert sc.check_range("main~1", "main", "feature/x") == []


def test_branch_session_parsing():
    assert sc.branch_session(f"sbx/{S}/work") == S
    assert sc.branch_session(f"refs/heads/sbx/{S}/a/b") == S
    for ref in (None, "", "main", "sbx/x", "feature/sbx/x/y"):
        assert sc.branch_session(ref) is None


def test_option_like_revisions_are_refused():
    with pytest.raises(ValueError):
        sc.check_range("--output=/tmp/x", "HEAD")


def test_checker_imports_without_third_party_dependencies():
    """CI (and, planned, the sandbox itself) must be able to run the checker
    without the launcher's dependencies: `invoke` arrives via sandbox_github."""
    import subprocess
    import sys
    code = ("import sys; sys.modules['invoke'] = None; sys.path.insert(0, %r); "
            "import sandbox_check, sandbox_actions; print('ok')"
            % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    r = subprocess.run([sys.executable, "-I", "-c", code],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_session_and_branch_rules_have_one_definition():
    import sandbox_actions
    import sandbox_names
    import sandbox_sessions
    assert sandbox_sessions.SESSION_RE is sandbox_names.SESSION_RE
    assert sandbox_actions.SESSION_RE is sandbox_names.SESSION_RE
    assert sandbox_sessions.BRANCH_ROOT == sandbox_names.BRANCH_ROOT == "sbx"
