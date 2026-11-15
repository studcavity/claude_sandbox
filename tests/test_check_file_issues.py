"""--file-issues: real git history, fake GitHub."""
import pytest

import sandbox_check as sc
from test_actions import Api, text
from test_check_rules import Repo


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Repo(tmp_path)


def run(api, **kw):
    lines = []
    rc = sc.file_issues_run("o/r", api=api, out=lines.append, **kw)
    return rc, lines


def issues(api):
    return api.posts("/issues")


def todo(repo, name, title, extra=None):
    return repo.commit(f"feat: {name}", {f"todo/{name}.md": text(title),
                                         **(extra or {})})


def test_todo_files_in_history_are_filed_oldest_first(repo):
    todo(repo, "first", "First item")
    repo.commit("feat: nothing to see", {"b.txt": "2"})
    todo(repo, "second", "Second item")
    api = Api(todos={"first": text("First item"), "second": text("Second item")})
    rc, lines = run(api)
    assert rc == 0
    assert [b["title"] for _, _, b in issues(api)] == ["First item", "Second item"]
    assert issues(api)[0][2]["labels"][:2] == ["sandbox", "follow-up"]
    assert "filed" in "\n".join(lines)


def test_only_files_a_commit_adds_are_requests(repo):
    todo(repo, "old", "Old")
    repo.commit("feat: edit it", {"todo/old.md": text("Old", "edited")})
    repo.commit("feat: not a request", {"todo/notes.txt": "x", "src/todo/a.md": "x"})
    api = Api()
    run(api)
    assert len(issues(api)) == 1


def test_a_file_deleted_before_the_scan_ends_is_still_one_request_per_add(repo):
    """The request is the commit that added the file: a later deletion
    doesn't matter (the file is read at that commit)."""
    todo(repo, "gone", "Gone")
    repo.commit("fix: resolved", remove=["todo/gone.md"])
    api = Api()
    run(api)
    assert [b["title"] for _, _, b in issues(api)] == ["Item gone"]


def test_a_path_added_again_after_deletion_is_a_new_request(repo):
    todo(repo, "fix-ci", "One")
    repo.commit("fix: resolved", remove=["todo/fix-ci.md"])
    todo(repo, "fix-ci", "Two")
    api = Api()
    run(api)
    assert len(issues(api)) == 2


def test_since_limits_the_range_and_last_limits_the_count(repo):
    todo(repo, "old", "Old")
    marker = repo.git("rev-parse", "HEAD").strip()
    todo(repo, "new", "New")
    api = Api()
    run(api, since=marker)
    assert [b["title"] for _, _, b in issues(api)] == ["Item new"]
    api2 = Api()
    run(api2, last=1)
    assert [b["title"] for _, _, b in issues(api2)] == ["Item new"]


def test_running_twice_does_not_duplicate(repo):
    todo(repo, "i1", "Once")
    api = Api()
    run(api)
    rc, lines = run(api)
    assert rc == 0 and len(issues(api)) == 1
    assert "already done" in "\n".join(lines)


def test_dry_run_reads_but_never_writes(repo):
    todo(repo, "i1", "Would be filed")
    api = Api(todos={"i1": text("Would be filed")})
    rc, lines = run(api, dry_run=True)
    assert rc == 0
    assert api.posts("/issues") == [] and api.posts("/comments") == []
    assert api.labels_created == []
    assert any(l.startswith("would file: 'Would be filed'") for l in lines)


def test_no_requests_says_so(repo):
    repo.commit("feat: plain", {"a.txt": "1"})
    rc, lines = run(Api())
    assert rc == 0 and "no todo/ requests found in the last 50 commits" in lines[-1]
    rc, lines = run(Api(), since="main~1")
    assert "in main~1..HEAD" in lines[-1]


def test_failures_set_the_exit_code(repo):
    todo(repo, "i1", "Boom")
    rc, lines = run(Api(fail_issue={"Item i1"}))
    assert rc == 1 and "FAILED" in "\n".join(lines)


def test_policy_rejections_are_reported_but_are_not_failures(repo):
    gh = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    todo(repo, "i1", "Leaky")
    api = Api(todos={"i1": text("Leaky", f"token {gh}")})
    rc, lines = run(api)
    assert rc == 0 and issues(api) == []
    assert "REJECTED (policy)" in "\n".join(lines) and gh not in "\n".join(lines)


@pytest.mark.parametrize("since", ["--output=/tmp/x", "-n1"])
def test_option_like_revisions_are_refused(repo, since):
    with pytest.raises(sc.CheckError):
        sc.read_commit_files(since=since)


def test_invalid_repo_is_refused_before_reading_anything(repo):
    with pytest.raises(ValueError):
        sc.file_issues_run("../etc", api=Api())


@pytest.mark.parametrize("argv", [
    ["--file-issues", "o/r", "main", "HEAD"], ["--file-issues", "o/r", "--last", "0"],
    ["--since", "HEAD~3"], ["--dry-run"],
])
def test_cli_combinations(argv):
    with pytest.raises(SystemExit) as e:
        sc.main(argv)
    assert e.value.code == 2


def test_file_issues_honours_the_limits(repo):
    repo.commit("feat: many", {f"todo/t{k}.md": text(f"T{k}") for k in range(5)})
    api = Api()
    run(api, limits={"actions": 2, "issues": 25})
    assert len(issues(api)) == 2


def test_cli_rejects_nonpositive_limits():
    with pytest.raises(SystemExit):
        sc.main(["--file-issues", "o/r", "--max-issues", "0"])
