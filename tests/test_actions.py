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
    msg(line(type="issue", id="x" * 101)),
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


# --- session cross-references and lint_commit ------------------------------ #
S = "quirky-hopper"


def nid(slug="smoke", session=S, date="20261115", rand="a3f9"):
    return f"{slug}-{date}-{session}-{rand}"


def commit(session=S, *lines, trailer=True):
    tr = [f"Sandbox-Session: {session}"] if trailer else []
    return msg(*lines, *tr)


def good(session=S, **kw):
    i = nid(session=session, **kw)
    return commit(session, issue(i)), {sa.todo_path(i)}, i


def test_a_fully_consistent_commit_lints_clean():
    m, files, _ = good()
    assert sa.lint_commit(m, files, S) == []
    assert sa.lint_commit(m, files) == []  # not a session branch: also fine


def test_session_branch_requires_the_trailer_and_it_must_match():
    m, files, _ = good()
    other = sa.lint_commit(m, files, "other-session")
    assert any("branch belongs to session 'other-session'" in p for p in other)
    assert any("missing 'Sandbox-Session: quirky-hopper'" in p for p in
               sa.lint_commit("subject\n\nbody\n\nCo-Authored-By: x\n",
                              set(), S))


@pytest.mark.parametrize("trailers,fragment", [
    (["Sandbox-Session: a", "Sandbox-Session: b"], "2 Sandbox-Session"),
    (["Sandbox-Session: bad name"], "invalid Sandbox-Session"),
    (["Sandbox-Session: "], "invalid Sandbox-Session"),
])
def test_bad_session_trailers_are_flagged(trailers, fragment):
    m = msg(*trailers)
    assert any(fragment in p for p in sa.lint_commit(m, set()))
    assert sa.message_session(m) is None


def test_action_session_must_match_the_commits_session_trailer():
    i = nid(session="someone-else")
    m = commit(S, issue(i))
    problems = sa.lint_commit(m, {sa.todo_path(i)}, S)
    assert any("must look like" in p and S in p for p in problems)


def test_actions_need_a_session_trailer_even_off_session_branches():
    i = nid()
    m = msg(issue(i))
    assert any("needs a valid Sandbox-Session" in p
               for p in sa.lint_commit(m, {sa.todo_path(i)}))


@pytest.mark.parametrize("bad", [
    "smoke", f"smoke-20261115-{S}", f"smoke-20261115-{S}-abc",
    f"smoke-20261115-{S}-abcde", f"smoke-20261115-{S}-ABCD",
    f"smoke-2026111-{S}-a3f9", f"-20261115-{S}-a3f9",
    "smoke-20261115-hopper-a3f9",        # a suffix of the session isn't it
    f"smoke-20261115-x{S}-a3f9",          # nor is a longer name
    f"smoke-20261340-{S}-a3f9",           # impossible date
])
def test_id_shape_is_enforced(bad):
    assert sa.id_problem(bad, S)


def test_id_shape_accepts_hyphenated_slugs_and_dotted_ids():
    assert sa.id_problem(nid(slug="run-smoke.sh_v2"), S) is None
    assert sa.id_problem(nid(rand="0z9a"), S) is None


def test_issue_actions_need_their_todo_file_in_the_same_commit():
    m, _, i = good()
    assert any(f"todo/{i}.md is not added" in p
               for p in sa.lint_commit(m, set(), S))
    assert any("is not added" in p
               for p in sa.lint_commit(m, {"todo/other.md"}, S))


def test_resolve_needs_no_todo_file_but_still_needs_a_good_id():
    r = nid(slug="done")
    m = commit(S, line(type="resolve", id=r, ref=f"{A[:7]}:x"))
    assert sa.lint_commit(m, set(), S) == []


def test_lint_flags_every_line_the_launcher_would_silently_drop():
    m = msg("Sandbox-Action: nope", "Sandbox-Action: [1]",
            line(type="mystery", id=nid(slug="m")),
            issue(nid()), issue(nid()), f"Sandbox-Session: {S}")
    problems = sa.lint_commit(m, {sa.todo_path(nid())}, S)
    text = "\n".join(problems)
    for frag in ("not one line of JSON", "not a JSON object", "unknown type",
                 "duplicate action id"):
        assert frag in text


def test_message_session():
    assert sa.message_session(msg(f"Sandbox-Session: {S}")) == S
    assert sa.message_session(msg("Co-Authored-By: x")) is None
    assert sa.message_session("") is None


class MismatchApi(RunApi):
    """Branch sbx/s/work holds three commits: one claiming session s, one
    claiming t, one with no claim."""
    def __call__(self, method, path, body=None, raw=False):
        if "/compare/main...sbx/s/work" in path:
            def c(sha, session):
                return {"sha": sha, "commit": {"message": commit(
                    session, issue(nid(session=session or "s")),
                    trailer=bool(session))}}
            return {"commits": [c(A, "s"), c(B, "t"), c("c" * 40, None)]}
        return super().__call__(method, path, body, raw)


def test_launcher_ignores_commits_claiming_another_session():
    out = sa.run("o/r", "s", api=MismatchApi())
    assert sorted(o.sha for o in out) == sorted([A, "c" * 40])


# --- the docs the sessions read agree with the code ------------------------ #
def test_template_examples_pass_the_lint(repo_root):
    """Render the real sandbox template for a session and lint the example
    action and session trailer it shows."""
    import os
    import re
    import string
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    text = string.Template(t).safe_substitute(session=S)
    (action_line,) = re.findall(r"`(Sandbox-Action: \{.*\})`", text)
    (session_line,) = re.findall(r"`(Sandbox-Session: [^`]+)`", text)
    (acts, problems) = sa.scan_message("", msg(action_line))
    assert problems == [] and len(acts) == 1
    m = msg(action_line, session_line)
    assert sa.lint_commit(m, {sa.todo_path(acts[0].id)}, S) == []
    assert S in text  # the session name is actually substituted in
