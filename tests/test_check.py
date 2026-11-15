"""Tests for sandbox_check against a real throwaway git repository (local
only: no network, no GitHub)."""
import os
import subprocess

import pytest

import sandbox_check as sc

TODO = "todo/fix-ci.md"
BRIEF = "# Fix the flaky CI job\n\nWhat went wrong and options.\n"


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

    def add(message, *paths, content="x"):
        for p in paths:
            f = tmp_path / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        (tmp_path / "n.txt").write_text(message)
        git("add", "-A")
        git("commit", "-q", "-m", message)
    return add


def errors():
    return sc.check_range("main~1", "main")


def test_a_valid_todo_file_passes(repo):
    repo("feat: x", TODO, content=BRIEF)
    assert errors() == []
    assert sc.main(["main~1", "main"]) == 0


@pytest.mark.parametrize("path,content,fragment", [
    ("todo/Fix_CI.md", BRIEF, "must be todo/<name>.md"),
    ("todo/sub/x.md", BRIEF, "must be todo/<name>.md"),
    (TODO, "no heading in here\n", "no heading"),
    (TODO, "# T\n" + "x" * 21000, "limit"),
])
def test_a_bad_todo_file_is_reported_with_the_sha(repo, capsys, path, content,
                                                  fragment):
    repo("feat: x", path, content=content)
    ((sha, problem),) = errors()
    assert len(sha) == 7 and path in problem and fragment in problem
    assert sc.main(["main~1", "main"]) == 1
    assert "::error::" in capsys.readouterr().out


def test_other_files_and_non_markdown_under_todo_are_not_requests(repo):
    repo("feat: x", "other.md", "todo/notes.txt", "docs/todo/Whatever.md",
         content="no heading")
    assert errors() == []


def test_deleting_a_todo_file_is_fine(repo, tmp_path):
    repo("feat: seed", TODO, content=BRIEF)
    (tmp_path / TODO).unlink()
    repo("feat: done")  # n.txt only
    assert sc.check_range("main~2", "main") == []


def test_editing_a_bad_todo_file_is_checked_again(repo, tmp_path):
    repo("feat: seed", TODO, content=BRIEF)
    repo("feat: edit", TODO, content="oops, no heading")
    assert [p for _, p in sc.check_range("main~1", "main")][0].endswith(
        "no heading; the first heading becomes the issue title")


def test_the_branch_name_is_not_an_input_any_more():
    with pytest.raises(SystemExit):
        sc.main(["a", "b", "--branch", "x"])


def test_option_like_revisions_are_refused():
    with pytest.raises(ValueError):
        sc.check_range("--output=/tmp/x", "HEAD")


def test_checker_imports_without_third_party_dependencies():
    """CI (and, planned, the sandbox itself) must be able to run the checker
    without the launcher's dependencies: `invoke` arrives via sandbox_github."""
    import subprocess
    import sys
    code = ("import sys; sys.modules['invoke'] = None; sys.path.insert(0, %r); "
            "import sandbox_check, sandbox_actions, sandbox_todo; print('ok')"
            % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    r = subprocess.run([sys.executable, "-I", "-c", code],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_session_and_branch_rules_have_one_definition():
    import sandbox_names
    import sandbox_sessions
    assert sandbox_sessions.SESSION_RE is sandbox_names.SESSION_RE
    assert sandbox_sessions.BRANCH_ROOT == sandbox_names.BRANCH_ROOT == "sbx"


def test_branch_session_parsing():
    import sandbox_names
    s = "quirky-hopper"
    assert sandbox_names.branch_session(f"sbx/{s}/work") == s
    assert sandbox_names.branch_session(f"refs/heads/sbx/{s}/a/b") == s
    for ref in (None, "", "main", "sbx/x", "feature/sbx/x/y"):
        assert sandbox_names.branch_session(ref) is None
