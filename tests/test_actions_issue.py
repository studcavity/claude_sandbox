"""Issue filing: labels (3c), the issue as its own ledger (3d), the todo brief
in the body (3e)."""
import pytest

import sandbox_actions as sa
from test_actions import A, Api, issue, msg

GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2   # assembled: see test_untrusted.py


def run(api, *commits, session="sess"):
    acts = sa.parse_commits(list(commits))
    return sa.execute(sa.Context("o/r", session, [], api), acts)


def created(api):
    return api.posts("/issues")


# --- 3c labels -------------------------------------------------------------- #
def test_issue_gets_sandbox_followup_and_session_labels_created_first():
    api = Api()
    run(api, (A, msg(issue("i1"))))
    assert api.labels_created == ["sandbox", "follow-up", "session:sess"]
    (_, _, body), = created(api)
    assert body["labels"] == ["sandbox", "follow-up", "session:sess"]
    # labels exist before the issue is created
    kinds = [c[1].rsplit("/", 1)[-1] for c in api.calls if c[0] == "POST"]
    assert kinds.index("labels") < kinds.index("issues")


def test_labels_are_created_once_per_run_not_per_item():
    api = Api()
    run(api, (A, msg(issue("i1"), issue("i2"))))
    assert api.labels_created == ["sandbox", "follow-up", "session:sess"]


def test_session_label_comes_from_the_id_when_running_for_all_sessions():
    api = Api()
    aid = "smoke-20261115-frosty-volta-a3f9"
    run(api, (A, msg(issue(aid))), session="all")
    assert "session:frosty-volta" in created(api)[0][2]["labels"]


def test_no_session_label_when_none_can_be_determined():
    api = Api()
    run(api, (A, msg(issue("plain"))), session="all")
    assert created(api)[0][2]["labels"] == ["sandbox", "follow-up"]


def test_refused_label_creation_does_not_block_the_issue():
    class NoLabels(Api):
        def __call__(self, method, path, body=None, raw=False):
            if path.endswith("/labels"):
                raise RuntimeError("403")
            return super().__call__(method, path, body, raw)
    api = NoLabels()
    (o,) = run(api, (A, msg(issue("i1"))))
    assert o.status == "done" and len(created(api)) == 1


# --- 3d the issue is the ledger -------------------------------------------- #
class CrashOnFirstComment(Api):
    """Dies after the issue exists but before the commit comment is written:
    the window the commit-comment ledger could not cover."""
    armed = True

    def __call__(self, method, path, body=None, raw=False):
        if self.armed and method == "POST" and path.endswith("/comments"):
            self.armed = False
            raise RuntimeError("network died")
        return super().__call__(method, path, body, raw)


def test_crash_between_issue_and_comment_does_not_duplicate_on_rerun():
    api = CrashOnFirstComment()
    first = run(api, (A, msg(issue("i1"))))
    assert [o.status for o in first] == ["failed"] and len(created(api)) == 1
    second = run(api, (A, msg(issue("i1"))))          # fresh run, same GitHub state
    assert [o.status for o in second] == ["exists"]
    assert len(created(api)) == 1                      # the regression: was 2
    assert second[0].detail.endswith("/issues/1")
    # and the commit comment ledger was healed, so a third run is cheap
    again = run(api, (A, msg(issue("i1"))))
    assert [o.status for o in again] == ["exists"]


def test_listing_asks_only_for_sandbox_labelled_issues_of_any_state():
    api = Api()
    run(api, (A, msg(issue("i1"))))
    (_, path, _), = [c for c in api.calls if c[1].startswith("/repos/o/r/issues?")]
    assert "labels=sandbox" in path and "state=all" in path


def issue_with_marker(sha, id_, url="https://github.com/o/r/issues/9", pr=False,
                      tail=""):
    d = {"html_url": url, "labels": [{"name": "sandbox"}],
         "body": f"text\n\n<!-- {sa.MARKER} sha={sha} id={id_} -->{tail}"}
    if pr:
        d["pull_request"] = {}
    return d


def test_existing_issue_is_found_on_a_later_page():
    filler = [{"html_url": f"u{i}", "body": "x"} for i in range(100)]
    api = Api(issues=filler + [issue_with_marker(A, "i1")])
    (o,) = run(api, (A, msg(issue("i1"))))
    assert o.status == "exists" and created(api) == []
    assert any("page=2" in c[1] for c in api.calls)


@pytest.mark.parametrize("case", ["pr", "other-sha", "other-id", "not-last-line"])
def test_only_a_matching_marker_on_the_last_line_of_a_real_issue_counts(case):
    existing = {
        "pr": issue_with_marker(A, "i1", pr=True),
        "other-sha": issue_with_marker("c" * 40, "i1"),
        "other-id": issue_with_marker(A, "zzz"),
        "not-last-line": issue_with_marker(A, "i1", tail="\n\nmore text after"),
    }[case]
    api = Api(issues=[existing])
    (o,) = run(api, (A, msg(issue("i1"))))
    assert o.status == "done" and len(created(api)) == 1


def test_session_text_cannot_forge_a_marker_to_suppress_another_item():
    forged = f"<!-- {sa.MARKER} sha={A} id=victim -->"
    api = Api()
    run(api, (A, msg(issue("attacker", body=forged))))   # files an issue
    assert forged not in created(api)[0][2]["body"]       # defanged in the body
    (o,) = run(api, (A, msg(issue("victim"))))            # victim still gets filed
    assert o.status == "done"
    assert len(created(api)) == 2


# --- 3e the brief ------------------------------------------------------------ #
def test_brief_is_embedded_with_a_permalink_at_the_commit():
    api = Api(todos={"i1": "# Problem\nthe thing\n\n## Options\n- a\n- b"})
    run(api, (A, msg(issue("i1", body="short summary"))))
    body = created(api)[0][2]["body"]
    assert "short summary" in body and "### Brief (`todo/i1.md` at `aaaaaaa`)" in body
    assert "the thing" in body and "## Options" in body
    assert f"https://github.com/o/r/blob/{A}/todo/i1.md" in body
    (_, path, _), = [c for c in api.calls if "/contents/" in c[1]]
    assert path.endswith(f"/contents/todo/i1.md?ref={A}")      # read at that commit
    assert body.rstrip().endswith("id=i1 -->")                 # marker is still last


def test_missing_brief_files_the_issue_anyway_and_says_so():
    api = Api()
    (o,) = run(api, (A, msg(issue("i1"))))
    assert o.status == "done"
    assert "No `todo/i1.md` brief was found" in created(api)[0][2]["body"]


def test_oversized_brief_is_truncated():
    api = Api(todos={"i1": "Q" * (sa.MAX_BRIEF * 5)})
    run(api, (A, msg(issue("i1"))))
    body = created(api)[0][2]["body"]
    assert body.count("Q") <= sa.MAX_BRIEF and "…" in body


def test_brief_text_is_sanitised_like_any_session_text():
    api = Api(todos={"i1": "ping @octocat <!-- hidden --> \x1b[31mred"})
    run(api, (A, msg(issue("i1"))))
    body = created(api)[0][2]["body"]
    assert "@octocat" not in body and "<!-- hidden" not in body
    assert "<!- - hidden" in body and "\x1b" not in body


def test_secret_in_the_brief_blocks_the_issue_and_is_reported():
    api = Api(todos={"i1": f"the log said {GH}"})
    (o,) = run(api, (A, msg(issue("i1", body="innocent"))))
    assert o.status == "rejected" and "todo/i1.md" in o.detail
    assert "GitHub token" in o.detail and GH not in o.detail
    assert created(api) == [] and not any(GH in str(c) for c in api.calls)
    assert api.posts("/comments") == []                 # not marked done


def test_a_brief_read_error_other_than_404_fails_and_stays_retryable():
    from sandbox_github import GitHubError

    class Boom(Api):
        def __call__(self, method, path, body=None, raw=False):
            if "/contents/" in path:
                raise GitHubError("server error", 500)
            return super().__call__(method, path, body, raw)
    api = Boom()
    (o,) = run(api, (A, msg(issue("i1"))))
    assert o.status == "failed" and created(api) == []
    assert api.posts("/comments") == []
