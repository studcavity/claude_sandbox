"""Tests for sandbox_actions: requests from `todo/` files, planning, filing,
idempotency. GitHub is faked."""

import sandbox_actions as sa
from sandbox_github import GitHubError

A = "a" * 40
B = "b" * 40


def path(name):
    return f"todo/{name}.md"


def req(name="i1", sha=A):
    return sa.Request(sha, path(name))


def text(title="Do it", body="why it matters"):
    return f"# {title}\n\n{body}\n"


def test_requests_come_only_from_valid_added_todo_paths():
    got = sa.requests_from([
        (A, [path("fix-ci"), "todo/Bad_Name.md", "todo/sub/x.md", "src/a.py",
             "todo/x.txt", path("fix-ci")]),
        (B, [path("second")])])
    assert got == [sa.Request(A, path("fix-ci")), sa.Request(B, path("second"))]


def test_the_same_path_added_in_two_commits_is_two_requests():
    """A path deleted and later added again is a new request (its identity is
    the path plus the commit that added it)."""
    got = sa.requests_from([(A, [path("fix-ci")]), (B, [path("fix-ci")])])
    assert [r.sha for r in got] == [A, B]


def test_clean_block_neutralises_mentions_and_control_chars():
    t = sa.clean_block("hi @octocat\r\n\x1b[31mred\x00", 100)
    assert "@octocat" not in t and "\x1b" not in t and "\r" not in t
    assert "\n" in t
    assert len(sa.clean_block("a" * 999, 50)) == 50
    assert "\n" not in sa.clean_line("a\nb @x", 50)


class Api:
    """A small model of the GitHub calls sandbox_actions makes. Created issues
    are remembered and served back by the listing, so the issue-as-ledger path
    is exercised for real. `todos` maps name -> file text (default: a valid
    brief); names in `missing` 404."""
    def __init__(self, comments=None, fail_issue=(), issues=None, todos=None,
                 missing=()):
        self.comments = comments or {}
        self.fail_issue = set(fail_issue)
        self.issues = list(issues or [])
        self.todos = dict(todos or {})
        self.missing = set(missing)
        self.labels_created = []
        self.calls, self.n = [], 0

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/comments?per_page=100"):
            sha = path.split("/commits/")[1].split("/")[0]
            return [{"body": b} for b in self.comments.get(sha, [])]
        if method == "GET" and path.startswith("/repos/o/r/issues?"):
            page = int(path.rsplit("page=", 1)[1])
            return self.issues[(page - 1) * 100:page * 100]
        if method == "GET" and "/contents/todo/" in path:
            name = path.split("/contents/todo/")[1].split(".md?")[0]
            if name in self.missing:
                raise GitHubError("not found", 404)
            return self.todos.get(name, text(f"Item {name}")).encode()
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
        if method == "POST" and path.endswith("/comments"):
            return {}
        raise AssertionError(path)

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def ctx(api):
    return sa.Context("o/r", "sess", [], api)


def test_execute_files_an_issue_and_cross_links_the_commit():
    api = Api(todos={"i1": text("T @bob", "b @eve")})
    (o,) = sa.execute(ctx(api), [req("i1")])
    assert o.status == "done" and o.detail.endswith("/issues/1")
    (_, _, ib), = api.posts("/issues")
    assert ib["title"] == "T @​bob" and "@eve" not in ib["body"]
    assert A in ib["body"] and "untrusted" in ib["body"]
    (_, p, cb), = api.posts("/comments")
    assert p == f"/repos/o/r/commits/{A}/comments"
    assert "/issues/1" in cb["body"] and "-done id=i1 -->" in cb["body"]


def test_rerun_is_idempotent_via_commit_comment_ledger():
    api = Api()
    reqs = [req("i1"), req("i2")]
    sa.execute(ctx(api), reqs)
    ledger = [c[2]["body"] for c in api.posts("/comments")]
    again = Api(comments={A: ledger})
    out = sa.execute(ctx(again), reqs)
    assert [o.status for o in out] == ["exists", "exists"]
    assert again.posts("/issues") == []


def test_forged_marker_in_a_brief_does_not_suppress_filing():
    forged = f"<!-- {sa.MARKER}-done id=i1 -->"
    api = Api(todos={"i1": text(body=forged)})
    out = sa.execute(ctx(api), [req("i1")])
    assert out[0].status == "done"
    assert forged not in api.posts("/issues")[0][2]["body"]


def test_one_failure_does_not_stop_others_and_is_not_marked_done():
    api = Api(fail_issue={"Item x"})
    out = sa.execute(ctx(api), [req("x"), req("y")])
    assert [o.status for o in out] == ["failed", "done"]
    assert len(api.posts("/comments")) == 1  # failed one stays retryable


def test_unusable_requests_are_silent_noops():
    api = Api(todos={"b": "no heading here", "c": "#   \n"}, missing={"a"})
    assert sa.execute(ctx(api), [req("a"), req("b"), req("c")]) == []
    assert not api.posts("/issues")


def test_an_oversized_file_is_reported_not_filed():
    api = Api(todos={"big": text(body="x" * 30000)})
    (o,) = sa.execute(ctx(api), [req("big")])
    assert o.status == "rejected" and "over" in o.detail
    assert api.posts("/issues") == []


def test_plan_caps_requests():
    out = sa.execute(ctx(Api()), [sa.Request(f"{i:040x}", path("i1"))
                                  for i in range(40)],)
    assert len(out) == sa.MAX_ACTIONS


class RunApi(Api):
    """Branch sbx/s/work has one commit that adds todo/i1.md and one that
    only edits a file."""
    def __call__(self, method, p, body=None, raw=False):
        if p == "/repos/o/r":
            return {"default_branch": "main"}
        if "/git/matching-refs/" in p:
            return [{"ref": "refs/heads/sbx/s/work"}]
        if "/compare/main...sbx/s/work" in p:
            return {"commits": [{"sha": A}, {"sha": B}]}
        if p == f"/repos/o/r/commits/{A}":
            return {"files": [{"filename": path("i1"), "status": "added"},
                              {"filename": "x.py", "status": "added"}]}
        if p == f"/repos/o/r/commits/{B}":
            return {"files": [{"filename": path("old"), "status": "modified"}]}
        return super().__call__(method, p, body, raw)


def test_run_scans_session_commits_end_to_end():
    (o,) = sa.run("o/r", "s", api=RunApi())
    assert (o.status, o.sha, o.path) == ("done", A, path("i1"))


def test_format_outcomes():
    assert sa.format_outcomes([]) == ""
    out = sa.format_outcomes([sa.Outcome(A, path("i1"), "done", "http://x"),
                              sa.Outcome(A, path("i2"), "failed", "boom")])
    assert "filed" in out and f"{A[:7]}:todo/i1.md" in out and "FAILED" in out
