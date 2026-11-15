"""Issue filing: labels, the issue as its own ledger, the todo file in the
body, caps."""
import pytest

import sandbox_actions as sa
from sandbox_github import GitHubError
from test_actions import A, Api, req, text

GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2   # assembled: see test_untrusted.py


def run(api, *requests, session="sess"):
    return sa.execute(sa.Context("o/r", session, [], api), list(requests))


def created(api):
    return api.posts("/issues")


# --- labels ---------------------------------------------------------------- #
def test_issue_gets_sandbox_followup_and_session_labels_created_first():
    api = Api()
    run(api, req("i1"))
    assert api.labels_created == ["sandbox", "follow-up", "session:sess"]
    (_, _, body), = created(api)
    assert body["labels"] == ["sandbox", "follow-up", "session:sess"]
    kinds = [c[1].rsplit("/", 1)[-1] for c in api.calls if c[0] == "POST"]
    assert kinds.index("labels") < kinds.index("issues")


def test_labels_are_created_once_per_run_not_per_item():
    api = Api()
    run(api, req("i1"), req("i2"))
    assert api.labels_created == ["sandbox", "follow-up", "session:sess"]


def test_no_session_label_when_running_for_all_sessions():
    api = Api()
    run(api, req("plain"), session="all")
    assert created(api)[0][2]["labels"] == ["sandbox", "follow-up"]


def test_refused_label_creation_does_not_block_the_issue():
    class NoLabels(Api):
        def __call__(self, method, p, body=None, raw=False):
            if p.endswith("/labels"):
                raise RuntimeError("403")
            return super().__call__(method, p, body, raw)
    api = NoLabels()
    (o,) = run(api, req("i1"))
    assert o.status == "done" and len(created(api)) == 1


# --- the issue is the ledger ----------------------------------------------- #
class CrashOnFirstComment(Api):
    """Dies after the issue exists but before the commit comment is written:
    the window the commit-comment ledger could not cover."""
    armed = True

    def __call__(self, method, p, body=None, raw=False):
        if self.armed and method == "POST" and p.endswith("/comments"):
            self.armed = False
            raise RuntimeError("network died")
        return super().__call__(method, p, body, raw)


def test_crash_between_issue_and_comment_does_not_duplicate_on_rerun():
    api = CrashOnFirstComment()
    first = run(api, req("i1"))
    assert [o.status for o in first] == ["failed"] and len(created(api)) == 1
    second = run(api, req("i1"))          # fresh run, same GitHub state
    assert [o.status for o in second] == ["exists"]
    assert len(created(api)) == 1
    assert second[0].detail.endswith("/issues/1")
    again = run(api, req("i1"))
    assert [o.status for o in again] == ["exists"]


def test_listing_asks_only_for_sandbox_labelled_issues_of_any_state():
    api = Api()
    run(api, req("i1"))
    (_, p, _), = [c for c in api.calls if c[1].startswith("/repos/o/r/issues?")]
    assert "labels=sandbox" in p and "state=all" in p


def issue_with_marker(sha, name, url="https://github.com/o/r/issues/9", pr=False,
                      tail=""):
    d = {"html_url": url, "labels": [{"name": "sandbox"}],
         "body": f"text\n\n<!-- {sa.MARKER} sha={sha} id={name} -->{tail}"}
    if pr:
        d["pull_request"] = {}
    return d


def test_existing_issue_is_found_on_a_later_page():
    filler = [{"html_url": f"u{i}", "body": "x"} for i in range(100)]
    api = Api(issues=filler + [issue_with_marker(A, "i1")])
    (o,) = run(api, req("i1"))
    assert o.status == "exists" and created(api) == []
    assert any("page=2" in c[1] for c in api.calls)


@pytest.mark.parametrize("case", ["pr", "other-sha", "other-name", "not-last-line"])
def test_only_a_matching_marker_on_the_last_line_of_a_real_issue_counts(case):
    existing = {
        "pr": issue_with_marker(A, "i1", pr=True),
        "other-sha": issue_with_marker("c" * 40, "i1"),
        "other-name": issue_with_marker(A, "zzz"),
        "not-last-line": issue_with_marker(A, "i1", tail="\n\nmore text after"),
    }[case]
    api = Api(issues=[existing])
    (o,) = run(api, req("i1"))
    assert o.status == "done" and len(created(api)) == 1


def test_the_same_path_added_again_in_a_later_commit_is_filed_again():
    api = Api()
    run(api, req("fix-ci", A))
    out = run(api, req("fix-ci", "b" * 40))
    assert [o.status for o in out] == ["done"] and len(created(api)) == 2


def test_session_text_cannot_forge_a_marker_to_suppress_another_item():
    forged = f"<!-- {sa.MARKER} sha={A} id=victim -->"
    api = Api(todos={"attacker": text(body=forged)})
    run(api, req("attacker"))
    assert forged not in created(api)[0][2]["body"]       # defanged in the body
    (o,) = run(api, req("victim"))                         # victim still gets filed
    assert o.status == "done" and len(created(api)) == 2


# --- the file is the issue --------------------------------------------------- #
def test_the_file_is_the_body_with_a_permalink_and_its_heading_the_title():
    api = Api(todos={"i1": "# Problem\nthe thing\n\n## Options\n- a\n- b"})
    run(api, req("i1"))
    (_, _, ib), = created(api)
    assert ib["title"] == "Problem"
    assert "the thing" in ib["body"] and "## Options" in ib["body"]
    assert f"https://github.com/o/r/blob/{A}/todo/i1.md" in ib["body"]
    (_, p, _), = [c for c in api.calls if "/contents/" in c[1]]
    assert p.endswith(f"/contents/todo/i1.md?ref={A}")    # read at that commit
    assert ib["body"].rstrip().endswith("id=i1 -->")      # marker is still last


def test_the_file_text_is_sanitised_like_any_session_text():
    api = Api(todos={"i1": "# Hi\nping @octocat <!-- hidden --> \x1b[31mred"})
    run(api, req("i1"))
    body = created(api)[0][2]["body"]
    assert "@octocat" not in body and "<!-- hidden" not in body
    assert "<!- - hidden" in body and "\x1b" not in body


def test_a_long_file_is_truncated():
    api = Api(todos={"i1": "# T\n" + "Q" * (sa.MAX_BODY * 2)})
    run(api, req("i1"))
    body = created(api)[0][2]["body"]
    assert body.count("Q") <= sa.MAX_BODY and "…" in body


def test_secret_in_the_file_blocks_the_issue_and_is_reported():
    api = Api(todos={"i1": text(body=f"the log said {GH}")})
    (o,) = run(api, req("i1"))
    assert o.status == "rejected" and "todo/i1.md" in o.detail
    assert "GitHub token" in o.detail and GH not in o.detail
    assert created(api) == [] and not any(GH in str(c) for c in api.calls)
    assert api.posts("/comments") == []                    # not marked done


def test_a_read_error_other_than_404_fails_and_stays_retryable():
    class Boom(Api):
        def __call__(self, method, p, body=None, raw=False):
            if "/contents/" in p:
                raise GitHubError("server error", 500)
            return super().__call__(method, p, body, raw)
    api = Boom()
    (o,) = run(api, req("i1"))
    assert o.status == "failed" and created(api) == []
    assert api.posts("/comments") == []


# --- caps ---------------------------------------------------------------------- #
def many(n, prefix="i"):
    return [req(f"{prefix}{k}") for k in range(n)]


def names_of(outcomes, status):
    return [o.path for o in outcomes if o.status == status]


def test_a_session_is_capped_and_the_excess_is_reported_not_silently_dropped():
    api = Api()
    out = run(api, *many(13), session="sess")
    assert len(names_of(out, "done")) == sa.MAX_ISSUES_PER_SESSION == 10
    rejected = [o for o in out if o.status == "rejected"]
    assert len(rejected) == 3 and "cap 10" in rejected[0].detail
    assert "--max-issues" in rejected[0].detail
    assert len(created(api)) == 10


def test_the_cap_counts_issues_already_filed_so_a_rerun_cannot_dodge_it():
    api = Api()
    run(api, *many(10), session="sess")                  # fills the cap
    again = run(api, *[req(f"later{k}", "b" * 40) for k in range(2)],
                session="sess")
    assert [o.status for o in again] == ["rejected", "rejected"]
    assert len(created(api)) == 10


def test_items_that_already_exist_are_not_blocked_by_the_cap():
    api = Api()
    run(api, *many(10), session="sess")
    out = run(api, *many(10), session="sess")             # same items again
    assert set(o.status for o in out) == {"exists"}


def test_the_cap_is_per_session_not_per_repo():
    api = Api()
    run(api, *many(10), session="one")
    out = run(api, *[req(f"x{k}", "b" * 40) for k in range(2)], session="two")
    assert names_of(out, "done") == ["todo/x0.md", "todo/x1.md"]


def test_limits_are_configurable():
    api = Api()
    out = sa.execute(sa.Context("o/r", "sess", [], api, None,
                                {"issues": 2, "actions": 4}), many(6))
    assert len(out) == 4                                       # --max-actions
    assert len(names_of(out, "done")) == 2                     # --max-issues
    assert len(names_of(out, "rejected")) == 2


def test_default_limits_apply_when_none_are_given():
    c = sa.Context("o/r", "s", [], None)
    assert sa.limit(c, "actions") == 25 and sa.limit(c, "issues") == 10
