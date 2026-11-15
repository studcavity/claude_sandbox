"""Tests for github_issues: requests from a git checkout, filing, the ledger.
GitHub is faked; git is real."""
import subprocess

import pytest

from claude_sandbox.github import issues as si
A = "a" * 40
B = "b" * 40
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2   # assembled: see test_untrusted.py


def path(name):
    return f"todo/{name}.md"


def text(title="Do it", body="why it matters"):
    return f"# {title}\n\n{body}\n"


def req(name="i1", sha=A, body=None):
    return si.Request(sha, path(name), text(f"Item {name}") if body is None
                      else body)


class Api:
    """A small model of the GitHub calls github_issues makes. Created issues
    are remembered and served back by the listing, so the issue-as-ledger path
    is exercised for real."""
    def __init__(self, fail_issue=(), issues=None):
        self.fail_issue = set(fail_issue)
        self.issues = list(issues or [])
        self.labels_created = []
        self.calls, self.n = [], 0

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/repos/o/r/issues?"):
            page = int(path.rsplit("page=", 1)[1])
            return self.issues[(page - 1) * 100:page * 100]
        if method == "POST" and path == "/repos/o/r/labels":
            self.labels_created.append(body["name"])
            return {}
        if path == "/repos/o/r/issues":
            if body["title"] in self.fail_issue:
                raise RuntimeError("boom")
            self.n += 1
            url = f"https://github.com/o/r/issues/{self.n}"
            self.issues.append({"html_url": url, "body": body["body"],
                                "labels": [{"name": l} for l in body["labels"]]})
            return {"html_url": url}
        raise AssertionError(path)

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def file(api, *requests, **kw):
    return si.execute(api, "o/r", list(requests), **kw)


# --- reading requests from git ---------------------------------------------- #
class Repo:
    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.commit("base", {"README": "r"})

    def git(self, *a):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "commit.gpgsign=false", *a], cwd=self.path, check=True,
            capture_output=True, text=True).stdout

    def commit(self, message, files=None, remove=()):
        for name, content in (files or {}).items():
            f = self.path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        for name in remove:
            (self.path / name).unlink()
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Repo(tmp_path)


def names(requests):
    return [r.path for r in requests]


def test_requests_are_the_valid_todo_files_each_commit_adds_oldest_first(repo):
    a = repo.commit("feat: a", {path("first"): text("One"), "src/a.py": "x"})
    repo.commit("feat: nothing", {"b.txt": "2"})
    b = repo.commit("feat: b", {path("second"): text("Two")})
    got = si.read_requests()
    assert [(r.sha, r.path) for r in got] == [(a, path("first")),
                                               (b, path("second"))]
    assert got[0].text == text("One")


def test_only_valid_names_in_the_todo_dir_are_requests(repo):
    repo.commit("feat: x", {"todo/Bad_Name.md": text(), "todo/sub/x.md": text(),
                            "todo/notes.txt": "x", "src/todo/a.md": text(),
                            path("ok"): text()})
    assert names(si.read_requests()) == [path("ok")]


def test_edits_never_make_a_request_only_adds_do(repo):
    repo.commit("feat: add", {path("old"): text("Old")})
    repo.commit("feat: edit", {path("old"): text("Old", "edited")})
    assert len(si.read_requests()) == 1


def test_a_deleted_file_is_still_read_at_the_commit_that_added_it(repo):
    repo.commit("feat: add", {path("gone"): text("Gone")})
    repo.commit("fix: resolved", remove=[path("gone")])
    (r,) = si.read_requests()
    assert r.text == text("Gone")


def test_a_path_added_again_after_deletion_is_a_new_request(repo):
    a = repo.commit("feat: add", {path("fix-ci"): text("One")})
    repo.commit("fix: resolved", remove=[path("fix-ci")])
    b = repo.commit("feat: again", {path("fix-ci"): text("Two")})
    assert [r.sha for r in si.read_requests()] == [a, b]


def test_an_oversized_file_is_read_as_none_not_loaded(repo):
    repo.commit("feat: big", {path("big"): "# T\n" + "x" * 30000})
    (r,) = si.read_requests()
    assert r.text is None
    (o,) = file(Api(), r)
    assert o.status == "rejected" and "over" in o.detail


def test_since_limits_the_range_and_last_limits_the_count(repo):
    repo.commit("feat: old", {path("old"): text("Old")})
    marker = repo.git("rev-parse", "HEAD").strip()
    repo.commit("feat: new", {path("new"): text("New")})
    assert names(si.read_requests(since=marker)) == [path("new")]
    assert names(si.read_requests(last=1)) == [path("new")]
    assert names(si.read_requests(last=2)) == [path("old"), path("new")]


@pytest.mark.parametrize("since", ["--output=/tmp/x", "-n1"])
def test_option_like_revisions_are_refused(repo, since):
    with pytest.raises(si.IssuesError):
        si.read_requests(since=since)


# --- filing ------------------------------------------------------------------ #
def test_clean_block_neutralises_mentions_and_control_chars():
    t = si.clean_block("hi @octocat\r\n\x1b[31mred\x00", 100)
    assert "@octocat" not in t and "\x1b" not in t and "\r" not in t
    assert "\n" in t
    assert len(si.clean_block("a" * 999, 50)) == 50
    assert "\n" not in si.clean_line("a\nb @x", 50)


def test_an_issue_is_filed_with_title_body_labels_and_a_last_line_marker():
    api = Api()
    (o,) = file(api, req("i1", body="# T @bob\n\nb @eve\n"))
    assert o.status == "done" and o.detail.endswith("/issues/1")
    (_, _, ib), = api.posts("/issues")
    assert ib["title"] == "T @​bob" and "@eve" not in ib["body"]
    assert ib["labels"] == ["sandbox", "follow-up"]
    assert A in ib["body"] and "untrusted" in ib["body"]
    assert f"https://github.com/o/r/blob/{A}/todo/i1.md" in ib["body"]
    assert ib["body"].splitlines()[-1] == f"<!-- {si.MARKER} sha={A} id=i1 -->"
    assert api.posts("/comments") == []        # the issue is the only ledger


def test_labels_are_created_once_per_run_before_the_issue():
    api = Api()
    file(api, req("i1"), req("i2"))
    assert api.labels_created == ["sandbox", "follow-up"]
    kinds = [c[1].rsplit("/", 1)[-1] for c in api.calls if c[0] == "POST"]
    assert kinds.index("labels") < kinds.index("issues")


def test_refused_label_creation_does_not_block_the_issue():
    class NoLabels(Api):
        def __call__(self, method, p, body=None, raw=False):
            if p.endswith("/labels"):
                raise RuntimeError("403")
            return super().__call__(method, p, body, raw)
    api = NoLabels()
    (o,) = file(api, req("i1"))
    assert o.status == "done" and len(api.posts("/issues")) == 1


def test_running_twice_over_the_same_requests_files_nothing_new():
    api = Api()
    reqs = [req("i1"), req("i2")]
    first = file(api, *reqs)
    second = file(api, *reqs)                   # a fresh run, same GitHub state
    assert [o.status for o in first] == ["done", "done"]
    assert [o.status for o in second] == ["exists", "exists"]
    assert len(api.posts("/issues")) == 2
    assert second[0].detail.endswith("/issues/1")


def test_duplicate_requests_in_one_run_are_filed_once():
    api = Api()
    out = file(api, req("i1"), req("i1"))
    assert [o.status for o in out] == ["done", "exists"]


def test_the_ledger_search_asks_only_for_follow_up_issues_of_any_state():
    api = Api()
    file(api, req("i1"))
    (_, p, _), = [c for c in api.calls if c[1].startswith("/repos/o/r/issues?")]
    assert "labels=follow-up" in p and "state=all" in p


def marked(sha, name, url="https://github.com/o/r/issues/9", pr=False, tail=""):
    d = {"html_url": url, "labels": [{"name": "follow-up"}],
         "body": f"text\n\n<!-- {si.MARKER} sha={sha} id={name} -->{tail}"}
    if pr:
        d["pull_request"] = {}
    return d


def test_an_existing_issue_is_found_on_a_later_page():
    filler = [{"html_url": f"u{i}", "body": "x"} for i in range(100)]
    api = Api(issues=filler + [marked(A, "i1")])
    (o,) = file(api, req("i1"))
    assert o.status == "exists" and api.posts("/issues") == []
    assert any("page=2" in c[1] for c in api.calls)


@pytest.mark.parametrize("case", ["pr", "other-sha", "other-name", "not-last-line"])
def test_only_a_matching_marker_on_the_last_line_of_a_real_issue_counts(case):
    existing = {
        "pr": marked(A, "i1", pr=True),
        "other-sha": marked("c" * 40, "i1"),
        "other-name": marked(A, "zzz"),
        "not-last-line": marked(A, "i1", tail="\n\nmore text after"),
    }[case]
    api = Api(issues=[existing])
    (o,) = file(api, req("i1"))
    assert o.status == "done" and len(api.posts("/issues")) == 1


def test_the_same_path_added_in_a_later_commit_is_filed_again():
    api = Api()
    file(api, req("fix-ci", A))
    out = file(api, req("fix-ci", B))
    assert [o.status for o in out] == ["done"] and len(api.posts("/issues")) == 2


def test_session_text_cannot_forge_a_marker_to_suppress_another_item():
    forged = f"<!-- {si.MARKER} sha={A} id=victim -->"
    api = Api()
    file(api, req("attacker", body=text(body=forged)))
    assert forged not in api.posts("/issues")[0][2]["body"]    # defanged
    (o,) = file(api, req("victim"))                            # still gets filed
    assert o.status == "done" and len(api.posts("/issues")) == 2


def test_one_failure_does_not_stop_the_others_and_stays_retryable():
    api = Api(fail_issue={"Item x"})
    out = file(api, req("x"), req("y"))
    assert [o.status for o in out] == ["failed", "done"]
    retry = file(Api(issues=api.issues), req("x"))
    assert [o.status for o in retry] == ["done"]


def test_a_crash_after_the_issue_exists_cannot_duplicate_it():
    """The issue and its marker are one write, so there is no window between
    'created' and 'recorded'."""
    api = Api()
    file(api, req("i1"))
    again = file(api, req("i1"))
    assert [o.status for o in again] == ["exists"] and len(api.posts("/issues")) == 1


def test_a_file_without_a_heading_is_a_silent_noop():
    api = Api()
    assert file(api, req("a", body="no heading\n"), req("b", body="#   \n")) == []
    assert api.posts("/issues") == []


def test_the_file_text_is_sanitised_like_any_session_text():
    api = Api()
    file(api, req("i1", body="# Hi\nping @octocat <!-- hidden --> \x1b[31mred"))
    body = api.posts("/issues")[0][2]["body"]
    assert "@octocat" not in body and "<!-- hidden" not in body
    assert "<!- - hidden" in body and "\x1b" not in body


def test_a_long_file_is_truncated():
    api = Api()
    file(api, req("i1", body="# T\n" + "Q" * (si.MAX_BODY * 2)))
    body = api.posts("/issues")[0][2]["body"]
    assert body.count("Q") <= si.MAX_BODY and "…" in body


def test_a_secret_blocks_the_issue_and_is_reported_without_echoing_it():
    api = Api()
    out = file(api, req("leak", body=text("Rotate it", f"found {GH} in the log")),
               req("fine"))
    by_path = {o.path: o for o in out}
    leak = by_path[path("leak")]
    assert leak.status == "rejected" and "GitHub token" in leak.detail
    assert GH not in leak.detail and by_path[path("fine")].status == "done"
    assert len(api.posts("/issues")) == 1
    assert not any(GH in str(c) for c in api.calls)


def test_a_secret_in_the_title_is_caught_too():
    api = Api()
    (o,) = file(api, req("t", body=f"# key {GH}\n"))
    assert o.status == "rejected" and api.posts("/issues") == []


def test_the_per_run_cap_bounds_what_one_run_files():
    api = Api()
    out = file(api, *[req(f"i{k}") for k in range(40)])
    assert len(out) == si.MAX_PER_RUN == 25
    out = file(Api(), *[req(f"i{k}") for k in range(6)], limit=4)
    assert len(out) == 4


def test_format_outcomes():
    assert si.format_outcomes([]) == ""
    out = si.format_outcomes([si.Outcome(A, path("i1"), "done", "http://x"),
                              si.Outcome(A, path("i2"), "failed", "boom"),
                              si.Outcome(A, path("i3"), "rejected", "leak")])
    assert "filed" in out and f"{A[:7]}:todo/i1.md" in out
    assert "FAILED" in out and "REJECTED (policy)" in out
