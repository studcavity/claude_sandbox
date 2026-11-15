"""Tests for sandbox_actions: the trailer spec, planning, idempotency."""
import json

import pytest

import sandbox_actions as sa

A = "a" * 40
B = "b" * 40


def line(**kw):
    return "Sandbox-Action: " + json.dumps({"v": 1, **kw})


def msg(*lines, subject="feat: x"):
    return subject + "\n\nbody text\n\n" + "\n".join(lines) + "\n"


def issue(id="i1", title="Do it", **kw):
    return line(type="issue", id=id, title=title, **kw)


def test_parses_a_conforming_trailer_among_other_trailers():
    m = msg(issue(body="why"), "Co-Authored-By: Claude <x@y>")
    (a,) = sa.parse_message(A, m)
    assert (a.sha, a.id, a.type, a.v) == (A, "i1", "issue", 1)
    assert a.data["body"] == "why"


def test_trailer_key_is_case_insensitive():
    m = msg(issue().replace("Sandbox-Action", "sandbox-action"))
    assert len(sa.parse_message(A, m)) == 1


@pytest.mark.parametrize("message", [
    issue(),                                     # no body paragraph at all
    "s\n\n" + issue() + "\n\nmore prose",        # not the last paragraph
    msg("Sandbox-Action: not json"),
    msg("Sandbox-Action: [1, 2]"),
    msg("Sandbox-Action: " + json.dumps({"type": "issue", "id": "x"})),  # no v
    msg('Sandbox-Action: {"v": "1", "type": "issue", "id": "x"}'),
    msg('Sandbox-Action: {"v": true, "type": "issue", "id": "x"}'),
    msg('Sandbox-Action: {"v": 1, "type": 5, "id": "x"}'),
    msg(line(type="issue", id="has space")),
    msg(line(type="issue", id="")),
    msg(line(type="issue", id="x" * 41)),
    msg(line(type="issue")),                     # no id
    msg("Sandbox-Action: " + "x" * (sa.MAX_LINE + 1)),
    msg(" " + issue()),                          # indented: continuation
    "",
])
def test_nonconforming_messages_are_noops(message):
    assert sa.parse_message(A, message) == []


def test_bad_lines_do_not_affect_good_ones_and_first_duplicate_wins():
    m = msg("Sandbox-Action: junk", issue("a", "first"), issue("a", "second"),
            issue("b", "other"))
    got = sa.parse_message(A, m)
    assert [(x.id, x.data["title"]) for x in got] == [("a", "first"),
                                                       ("b", "other")]


def test_unknown_keys_types_and_versions_parse_but_never_run():
    m = msg(line(type="future", id="f"), line(type="issue", id="g", title="t",
                                              v2_field=1),
            json.dumps({}) and 'Sandbox-Action: {"v": 2, "type": "issue", '
            '"id": "h", "title": "t"}')
    acts = sa.parse_commits([(A, m)])
    assert len(acts) == 3
    to_run, _ = sa.plan(acts)
    assert [a.id for a in to_run] == ["g"]  # only (issue, 1) is understood


def test_parse_commits_dedupes_shared_shas():
    m = msg(issue())
    assert len(sa.parse_commits([(A, m), (A, m)])) == 1


def test_resolve_supersedes_earlier_item_by_prefix():
    acts = sa.parse_commits([
        (A, msg(issue("i1"), issue("i2"))),
        (B, msg(line(type="resolve", id="r", ref=f"{A[:7]}:i1")))])
    to_run, skipped = sa.plan(acts)
    assert [a.id for a in to_run] == ["i2"]
    assert [a.id for a in skipped] == ["i1"]


@pytest.mark.parametrize("ref", ["", "i1", "zzzzzzz:i1", f"{A[:6]}:i1",
                                 f"{B[:7]}:i1", 5, f"{'c' * 7}:i1"])
def test_bad_ambiguous_or_self_resolves_change_nothing(ref):
    acts = sa.parse_commits([
        (A, msg(issue("i1"))),
        (B, msg(line(type="resolve", id="r", ref=ref)))])
    to_run, skipped = sa.plan(acts)
    assert [a.id for a in to_run] == ["i1"] and skipped == []


def test_ambiguous_prefix_is_a_noop():
    a2 = "a" * 7 + "9" * 33
    acts = sa.parse_commits([(A, msg(issue("i1"))), (a2, msg(issue("i1"))),
                             (B, msg(line(type="resolve", id="r",
                                          ref=f"{A[:7]}:i1")))])
    assert len(sa.plan(acts)[0]) == 2


def test_plan_caps_actions():
    acts = sa.parse_commits([(f"{i:040x}", msg(issue())) for i in range(40)])
    assert len(sa.plan(acts)[0]) == sa.MAX_ACTIONS


def test_clean_block_neutralises_mentions_and_control_chars():
    t = sa.clean_block("hi @octocat\r\n\x1b[31mred\x00", 100)
    assert "@octocat" not in t and "\x1b" not in t and "\r" not in t
    assert "\n" in t
    assert len(sa.clean_block("a" * 999, 50)) == 50
    assert "\n" not in sa.clean_line("a\nb @x", 50)


class Api:
    def __init__(self, comments=None, fail_issue=()):
        self.comments = comments or {}
        self.fail_issue = set(fail_issue)
        self.calls, self.n = [], 0

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/comments?per_page=100"):
            sha = path.split("/commits/")[1].split("/")[0]
            return [{"body": b} for b in self.comments.get(sha, [])]
        if path == "/repos/o/r/issues":
            if body["title"] in self.fail_issue:
                raise RuntimeError("boom")
            self.n += 1
            return {"html_url": f"https://github.com/o/r/issues/{self.n}"}
        if method == "POST" and path.endswith("/comments"):
            return {}
        raise AssertionError(path)

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


def ctx(api):
    return sa.Context("o/r", "sess", [], api)


def test_execute_files_issue_and_cross_links_commit():
    api = Api()
    acts = sa.parse_commits([(A, msg(issue("i1", "T @bob", body="b @eve")))])
    (o,) = sa.execute(ctx(api), acts)
    assert o.status == "done" and o.detail.endswith("/issues/1")
    (_, _, ib), = api.posts("/issues")
    assert ib["title"] == "T @​bob" and "@eve" not in ib["body"]
    assert A in ib["body"] and "untrusted" in ib["body"]
    (_, path, cb), = api.posts("/comments")
    assert path == f"/repos/o/r/commits/{A}/comments"
    assert "/issues/1" in cb["body"] and "-done id=i1 -->" in cb["body"]


def test_rerun_is_idempotent_via_commit_comment_ledger():
    api = Api()
    acts = sa.parse_commits([(A, msg(issue("i1"), issue("i2")))])
    sa.execute(ctx(api), acts)
    ledger = [c[2]["body"] for c in api.posts("/comments")]
    again = Api(comments={A: ledger})
    out = sa.execute(ctx(again), acts)
    assert [o.status for o in out] == ["exists", "exists"]
    assert again.posts("/issues") == []


def test_forged_marker_in_a_commit_message_does_not_suppress_filing():
    forged = f"<!-- {sa.MARKER}-done id=i1 -->"
    api = Api()
    out = sa.execute(ctx(api), sa.parse_commits(
        [(A, msg(issue("i1", body=forged)))]))
    assert out[0].status == "done"


def test_one_failure_does_not_stop_others_and_is_not_marked_done():
    api = Api(fail_issue={"bad"})
    acts = sa.parse_commits([(A, msg(issue("x", "bad"), issue("y", "ok")))])
    out = sa.execute(ctx(api), acts)
    assert [o.status for o in out] == ["failed", "done"]
    assert len(api.posts("/comments")) == 1  # failed one stays retryable


def test_rejected_payloads_are_silent_noops():
    api = Api()
    acts = sa.parse_commits([(A, msg(
        line(type="issue", id="a"), line(type="issue", id="b", title="  "),
        line(type="issue", id="c", title="t", body=5)))])
    assert sa.execute(ctx(api), acts) == []
    assert api.calls == [("GET", f"/repos/o/r/commits/{A}/comments?per_page=100",
                          None)]


def test_superseded_items_are_reported_and_not_filed():
    api = Api()
    acts = sa.parse_commits([
        (A, msg(issue("i1"))),
        (B, msg(line(type="resolve", id="r", ref=f"{A[:7]}:i1")))])
    out = sa.execute(ctx(api), acts)
    assert [o.status for o in out] == ["superseded"]
    assert api.posts("/issues") == []


class RunApi(Api):
    def __call__(self, method, path, body=None, raw=False):
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if "/git/matching-refs/" in path:
            return [{"ref": "refs/heads/sbx/s/work"}]
        if "/compare/main...sbx/s/work" in path:
            return {"commits": [{"sha": A, "commit": {"message": msg(issue())}}]}
        return super().__call__(method, path, body, raw)


def test_run_scans_session_commits_end_to_end():
    api = RunApi()
    (o,) = sa.run("o/r", "s", api=api)
    assert o.status == "done" and o.sha == A


def test_format_outcomes():
    assert sa.format_outcomes([]) == ""
    text = sa.format_outcomes([sa.Outcome(A, "i1", "issue", "done", "http://x"),
                               sa.Outcome(A, "i2", "issue", "failed", "boom")])
    assert "filed" in text and f"{A[:7]}:i1" in text and "FAILED" in text
