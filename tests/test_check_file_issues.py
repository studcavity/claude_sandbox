"""--file-issues (plan 5e): real git history, fake GitHub."""
import pytest

import sandbox_check as sc
from test_actions import Api, issue, msg
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


def test_issue_trailers_in_history_are_filed_oldest_first(repo):
    repo.commit(msg(issue("first", "First item")), {"a.txt": "1"})
    repo.commit("feat: nothing to see", {"b.txt": "2"})
    repo.commit(msg(issue("second", "Second item")), {"c.txt": "3"})
    api = Api()
    rc, lines = run(api)
    assert rc == 0
    assert [b["title"] for _, _, b in issues(api)] == ["First item", "Second item"]
    assert issues(api)[0][2]["labels"][:2] == ["sandbox", "follow-up"]
    assert "filed" in "\n".join(lines)


def test_since_limits_the_range_and_last_limits_the_count(repo):
    repo.commit(msg(issue("old", "Old")), {"a.txt": "1"})
    marker = repo.git("rev-parse", "HEAD").strip()
    repo.commit(msg(issue("new", "New")), {"b.txt": "2"})
    api = Api()
    run(api, since=marker)
    assert [b["title"] for _, _, b in issues(api)] == ["New"]
    api2 = Api()
    run(api2, last=1)
    assert [b["title"] for _, _, b in issues(api2)] == ["New"]


def test_running_twice_does_not_duplicate(repo):
    repo.commit(msg(issue("i1", "Once")), {"a.txt": "1"})
    api = Api()
    run(api)
    rc, lines = run(api)
    assert rc == 0 and len(issues(api)) == 1
    assert "already done" in "\n".join(lines)


def test_dry_run_reads_but_never_writes(repo):
    repo.commit(msg(issue("i1", "Would be filed")), {"a.txt": "1"})
    api = Api()
    rc, lines = run(api, dry_run=True)
    assert rc == 0
    assert api.posts("/issues") == [] and api.posts("/comments") == []
    assert api.labels_created == []
    assert any(l.startswith("would file: 'Would be filed'") for l in lines)


def test_a_later_resolve_commit_suppresses_the_item(repo):
    first = repo.commit(msg(issue("i1", "Fixed since")), {"a.txt": "1"})
    resolve = ('Sandbox-Action: {"v": 1, "type": "resolve", "id": "r1", '
               f'"ref": "{first[:10]}:i1"}}')
    repo.commit("fix: done\n\nbody\n\n" + resolve, {"b.txt": "2"})
    api = Api()
    rc, lines = run(api)
    assert issues(api) == [] and "resolved by a later commit" in "\n".join(lines)


def test_no_trailers_says_so(repo):
    repo.commit("feat: plain", {"a.txt": "1"})
    rc, lines = run(Api())
    assert rc == 0 and "no Sandbox-Action trailers found in the last 50 commits" in lines[-1]
    rc, lines = run(Api(), since="main~1")
    assert "in main~1..HEAD" in lines[-1]


def test_failures_set_the_exit_code(repo):
    repo.commit(msg(issue("i1", "Boom")), {"a.txt": "1"})
    rc, lines = run(Api(fail_issue={"Boom"}))
    assert rc == 1 and "FAILED" in "\n".join(lines)


def test_policy_rejections_are_reported_but_are_not_failures(repo):
    gh = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    repo.commit(msg(issue("i1", "Leaky", body=f"token {gh}")), {"a.txt": "1"})
    api = Api()
    rc, lines = run(api)
    assert rc == 0 and issues(api) == []
    assert "REJECTED (policy)" in "\n".join(lines) and gh not in "\n".join(lines)


@pytest.mark.parametrize("since", ["--output=/tmp/x", "-n1"])
def test_option_like_revisions_are_refused(repo, since):
    with pytest.raises(sc.CheckError):
        sc.read_commits(since=since)


def test_invalid_repo_is_refused_before_reading_anything(repo):
    with pytest.raises(ValueError):
        sc.file_issues_run("../etc", api=Api())


@pytest.mark.parametrize("argv", [
    ["--file-issues", "o/r", "main", "HEAD"], ["--file-issues", "o/r", "--branch", "x"],
    ["--file-issues", "o/r", "--post-status", "o/r"], ["--file-issues", "o/r", "--last", "0"],
    ["--since", "HEAD~3"], ["--dry-run"],
])
def test_cli_combinations(argv):
    with pytest.raises(SystemExit) as e:
        sc.main(argv)
    assert e.value.code == 2
