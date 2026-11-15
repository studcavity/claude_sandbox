"""The sandbox_issues command line (`run` and `main`): real git, fake GitHub."""
import pytest

import sandbox_issues as si
from test_issues import Api, GH, Repo, path, text


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Repo(tmp_path)


def run(api, **kw):
    lines = []
    rc = si.run("o/r", api=api, out=lines.append, **kw)
    return rc, lines


def issues(api):
    return api.posts("/issues")


def add(repo, name, title):
    return repo.commit(f"feat: {name}", {path(name): text(title)})


def test_todo_files_in_history_are_filed_oldest_first(repo):
    add(repo, "first", "First item")
    repo.commit("feat: nothing to see", {"b.txt": "2"})
    add(repo, "second", "Second item")
    api = Api()
    rc, lines = run(api)
    assert rc == 0
    assert [b["title"] for _, _, b in issues(api)] == ["First item", "Second item"]
    assert "filed" in "\n".join(lines)


def test_running_twice_does_not_duplicate(repo):
    add(repo, "i1", "Once")
    api = Api()
    run(api)
    rc, lines = run(api)
    assert rc == 0 and len(issues(api)) == 1
    assert "already filed" in "\n".join(lines)


def test_since_and_last_choose_the_range(repo):
    add(repo, "old", "Old")
    marker = repo.git("rev-parse", "HEAD").strip()
    add(repo, "new", "New")
    api = Api()
    run(api, since=marker)
    assert [b["title"] for _, _, b in issues(api)] == ["New"]
    api2 = Api()
    run(api2, last=1)
    assert [b["title"] for _, _, b in issues(api2)] == ["New"]


def test_dry_run_reads_but_never_writes(repo):
    add(repo, "i1", "Would be filed")
    api = Api()
    rc, lines = run(api, dry_run=True)
    assert rc == 0
    assert issues(api) == [] and api.labels_created == []
    assert any(l.startswith("would file: 'Would be filed'") for l in lines)


def test_no_requests_says_so(repo):
    repo.commit("feat: plain", {"a.txt": "1"})
    rc, lines = run(Api())
    assert rc == 0 and "no todo/ requests found in the last 50 commits" in lines[-1]
    rc, lines = run(Api(), since="main~1")
    assert "in main~1..HEAD" in lines[-1]


def test_failures_set_the_exit_code(repo):
    add(repo, "i1", "Boom")
    rc, lines = run(Api(fail_issue={"Boom"}))
    assert rc == 1 and "FAILED" in "\n".join(lines)


def test_policy_rejections_are_reported_but_are_not_failures(repo):
    repo.commit("feat: leak", {path("i1"): text("Leaky", f"token {GH}")})
    api = Api()
    rc, lines = run(api)
    assert rc == 0 and issues(api) == []
    assert "REJECTED (policy)" in "\n".join(lines) and GH not in "\n".join(lines)


def test_the_cap_applies(repo):
    repo.commit("feat: many", {path(f"t{k}"): text(f"T{k}") for k in range(5)})
    api = Api()
    run(api, limit=2)
    assert len(issues(api)) == 2


def test_an_invalid_repo_is_refused_before_reading_anything(repo):
    with pytest.raises(ValueError):
        si.run("../etc", api=Api())


@pytest.mark.parametrize("argv", [["o/r", "--last", "0"], ["o/r", "--max", "0"],
                                  []])
def test_cli_rejects_bad_arguments(argv):
    with pytest.raises(SystemExit) as e:
        si.main(argv)
    assert e.value.code == 2


def test_main_reports_a_bad_revision_as_an_error(repo, capsys):
    assert si.main(["o/r", "--since=--output=x"]) == 1
    assert "not a revision" in capsys.readouterr().err
