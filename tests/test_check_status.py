"""--post-status / --override (plan 5c): the PR path runs through real git, with
a local repository standing in for GitHub and a fake API for the REST calls."""
import dataclasses
import json

import pytest

import sandbox_check as sc
from sandbox_ghclient import PrInfo
from sandbox_github import GitHubError
from test_check_rules import GH, Repo

LOGIN = "studcavity"


# --- pure pieces ------------------------------------------------------------ #
def F(sev, text="t", sha="abc1234", path=None, line=None):
    return sc.Finding(sha, sev, text, path, line)


@pytest.mark.parametrize("findings,strict,state,desc", [
    ([], False, "success", "0 errors, 0 warnings"),
    ([F(sc.WARNING)], False, "success", "0 errors, 1 warning"),
    ([F(sc.WARNING)], True, "failure", "0 errors, 1 warning"),
    ([F(sc.ERROR), F(sc.WARNING), F(sc.WARNING)], False, "failure", "1 error, 2 warnings"),
    ([F(sc.NOTICE)], True, "success", "0 errors, 0 warnings"),
])
def test_status_for(findings, strict, state, desc):
    assert sc.status_for(findings, strict) == (state, desc)


def test_override_description_format_and_bounds():
    d = sc.override_description(LOGIN, "false positive: the fixture is fake")
    assert d == f"OVERRIDDEN by {LOGIN}: false positive: the fixture is fake"
    long = sc.override_description(LOGIN, "x" * 500)
    assert len(long) == sc.STATUS_DESC_MAX and long.endswith("…")
    assert "\n" not in sc.override_description(LOGIN, "line one\nline two is here")
    assert sc.override_description("bad login!", "a long enough reason").startswith(
        "OVERRIDDEN by unknown:")


@pytest.mark.parametrize("reason", ["", "short", "         ", "\n\n\n\n\n\n\n\n\n\n\n"])
def test_override_needs_a_real_reason(reason):
    with pytest.raises(sc.CheckError, match="real reason"):
        sc.override_description(LOGIN, reason)


def test_override_reason_with_a_credential_is_refused_without_echoing_it():
    with pytest.raises(sc.CheckError) as e:
        sc.override_description(LOGIN, f"because {GH} is needed")
    assert GH not in str(e.value) and "credential" in str(e.value)


def test_override_comment_is_auditable_and_defanged():
    body = sc.override_comment(LOGIN, "ping @octocat: fixture", "a" * 40,
                               [F(sc.ERROR, "bad thing"), F(sc.WARNING, "meh"),
                                F(sc.NOTICE, "ignored")])
    assert LOGIN in body and "aaaaaaa" in body and "bad thing" in body
    assert "1 error, 1 warning" in body and "ignored" not in body
    assert "@octocat" not in body and "this commit only" in body
    crashed = sc.override_comment(LOGIN, "checker is broken today", "a" * 40, None)
    assert "checker itself failed" in crashed


def test_override_comment_caps_the_findings_list():
    body = sc.override_comment(LOGIN, "many findings here", "a" * 40,
                               [F(sc.ERROR, f"e{i}") for i in range(50)])
    assert body.count("\n- ") == 21 and "and 30 more" in body


# --- the PR path, against a local "remote" ---------------------------------- #
class Api:
    """A fake typed client (sandbox_ghclient.GhClient). It records in the shapes
    the earlier REST-level fake used (path, body) so assertions read the same."""
    def __init__(self, base, head, ref, repo="o/r", head_after=None, fail_post=False):
        self.info = PrInfo(1, base, head, ref, repo, "https://github.com/o/r/pull/1")
        self.head_after, self.fail_post = head_after, fail_post
        self.reads, self.posts = 0, []
        self.label_calls, self.deletes, self.fail_labels = [], [], False

    def pr(self, repo, number):
        self.reads += 1
        if self.head_after and self.reads > 1:
            return dataclasses.replace(self.info, head_sha=self.head_after)
        return self.info

    def login(self):
        return LOGIN

    def post_status(self, repo, sha, state, context, description, target_url=None):
        if self.fail_post:
            raise GitHubError("HTTP 500", 500)
        body = {"state": state, "context": context, "description": description}
        if target_url:
            body["target_url"] = target_url
        self.posts.append((f"/repos/{repo}/statuses/{sha}", body))

    def comment(self, repo, number, body):
        self.posts.append((f"/repos/{repo}/issues/{number}/comments", {"body": body}))

    def ensure_label(self, repo, name, color, description):
        if self.fail_labels:
            raise GitHubError("HTTP 403", 403)

    def add_label(self, repo, number, name):
        if self.fail_labels:
            raise GitHubError("HTTP 403", 403)
        self.label_calls.append((f"/repos/{repo}/issues/{number}/labels", {"labels": [name]}))

    def remove_label(self, repo, number, name):
        if self.fail_labels:
            raise GitHubError("HTTP 403", 403)
        self.deletes.append((f"/repos/{repo}/issues/{number}/labels/{name}", None))

    def status(self):
        (s,) = [b for p, b in self.posts if "/statuses/" in p]
        return s


@pytest.fixture
def pr(tmp_path):
    """A repo with main + a PR branch; returns a builder(messages)->(Api, url)."""
    def build(messages, ref="feature/x", **kw):
        origin = Repo(tmp_path / "origin")
        base = origin.git("rev-parse", "HEAD").strip()
        origin.git("switch", "-q", "-c", "feature")
        for i, m in enumerate(messages):
            origin.commit(m, {f"f{i}.txt": str(i)})
        head = origin.git("rev-parse", "HEAD").strip()
        origin.git("update-ref", "refs/pull/1/head", head)
        return Api(base, head, ref, **kw), str(origin.path), head
    (tmp_path / "origin").mkdir()
    return build


def go(api, url, **kw):
    lines = []
    rc = sc.post_status_run("o/r", pr=1, client=api, url=url, out=lines.append, **kw)
    return rc, lines


def test_clean_pr_gets_a_success_status_on_the_head_sha(pr):
    api, url, head = pr(["feat: one", "fix: two"])
    rc, lines = go(api, url)
    assert rc == 0
    (path, body), = api.posts
    assert path == f"/repos/o/r/statuses/{head}"
    assert body == {"state": "success", "context": "sandbox/check",
                    "description": "0 errors, 0 warnings",
                    "target_url": "https://github.com/o/r/pull/1"}
    assert lines[-1].startswith("posted sandbox/check success on ")


def test_a_failing_pr_gets_a_failure_status_and_annotations(pr):
    api, url, _ = pr(["feat: ok", "fixup! feat: ok"])
    rc, lines = go(api, url)
    assert rc == 1 and api.status()["state"] == "failure"
    assert api.status()["description"] == "1 error, 0 warnings"
    assert any(l.startswith("::error::") and "fixup" in l for l in lines)


def test_branch_comes_from_the_pr_so_ids_must_name_its_session(pr):
    other = ('feat: x\n\nbody\n\nSandbox-Action: {"v": 1, "type": "resolve", '
             '"id": "done-20261115-someone-else-a3f9", "ref": "abcdef1:x"}')
    api, url, _ = pr([other], ref="sbx/quirky-hopper/work")
    rc, _ = go(api, url)
    assert rc == 1 and api.status()["state"] == "failure"      # id names another session


def test_secret_added_by_the_pr_fails_the_gate_without_echoing_it(pr, tmp_path):
    api, url, _ = pr(["feat: x"])
    # add a second commit carrying a credential, then repoint the PR ref
    o = Repo.__new__(Repo)
    o.path = tmp_path / "origin"
    sha = o.commit("feat: leak", {"leak.txt": f"T={GH}\n"})
    o.git("update-ref", "refs/pull/1/head", sha)
    api.info = dataclasses.replace(api.info, head_sha=sha)
    rc, lines = go(api, url)
    assert rc == 1 and not any(GH in l for l in lines)
    assert api.status()["state"] == "failure"


def test_a_crashing_checker_posts_error_never_success(pr, monkeypatch):
    api, url, _ = pr(["feat: x"])
    monkeypatch.setattr(sc, "check_findings", lambda *a, **k: 1 / 0)
    rc, lines = go(api, url)
    assert rc == 2
    assert api.status() == {"state": "error", "context": "sandbox/check",
                            "description": "check crashed: see the log",
                            "target_url": "https://github.com/o/r/pull/1"}
    assert any("crashed: ZeroDivisionError" in l for l in lines)


def test_nothing_is_posted_if_the_pr_head_moved_while_checking(pr):
    api, url, _ = pr(["feat: x"], head_after="f" * 40)
    with pytest.raises(sc.CheckError, match="head moved"):
        go(api, url)
    assert api.posts == []


def test_prs_from_forks_are_refused(pr):
    api, url, _ = pr(["feat: x"], repo="someone-else/r")
    with pytest.raises(sc.CheckError, match="fork|someone-else"):
        go(api, url)
    assert api.posts == []


def test_a_failed_fetch_is_a_clean_error_not_a_traceback(pr, tmp_path):
    api, _, _ = pr(["feat: x"])
    with pytest.raises(sc.CheckError, match="couldn't fetch PR #1"):
        go(api, str(tmp_path / "does-not-exist"))
    assert api.posts == []


def test_posting_failure_is_reported_not_swallowed(pr):
    api, url, _ = pr(["feat: x"], fail_post=True)
    with pytest.raises(sc.CheckError, match="couldn't post"):
        go(api, url)


@pytest.mark.parametrize("bad", ["--upload-pack=touch /tmp/x", "abc", "", None, "g" * 40])
def test_base_commit_id_from_the_api_is_validated_before_reaching_git(bad, tmp_path):
    with pytest.raises(sc.CheckError, match="unexpected base"):
        sc.fetch_pr("file:///nowhere", 1, bad, str(tmp_path / "d"))


# --- override (break glass) -------------------------------------------------- #
def test_override_posts_success_for_that_sha_and_comments_on_the_pr(pr):
    api, url, head = pr(["feat: ok", "fixup! feat: ok"])   # would fail the check
    rc, _ = go(api, url, override="false positive: reviewed by hand")
    assert rc == 0
    st = api.status()
    assert st["state"] == "success" and st["context"] == "sandbox/check"
    assert st["description"] == f"OVERRIDDEN by {LOGIN}: false positive: reviewed by hand"
    (cpath, cbody), = [(p, b) for p, b in api.posts if "/comments" in p]
    assert cpath == "/repos/o/r/issues/1/comments"
    assert LOGIN in cbody["body"] and "fixup" in cbody["body"] and head[:7] in cbody["body"]


def test_override_works_even_if_the_checker_itself_is_broken(pr, monkeypatch):
    api, url, _ = pr(["feat: x"])
    monkeypatch.setattr(sc, "check_findings", lambda *a, **k: 1 / 0)
    rc, _ = go(api, url, override="the checker is broken today")
    assert rc == 0 and api.status()["state"] == "success"
    (_, c), = [(p, b) for p, b in api.posts if "/comments" in p]
    assert "checker itself failed" in c["body"]


def test_a_bad_override_reason_posts_nothing(pr):
    api, url, _ = pr(["feat: x"])
    with pytest.raises(sc.CheckError):
        go(api, url, override="meh")
    assert api.posts == []


def test_override_requires_a_pr():
    with pytest.raises(sc.CheckError, match="needs --pr"):
        sc.post_status_run("o/r", base="a", head="b", override="a good reason here",
                           client=object())


# --- local mode and the CLI -------------------------------------------------- #
def test_local_range_posts_to_the_resolved_head_sha(tmp_path, monkeypatch):
    r = Repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    r.commit("feat: a", {"a.txt": "1"})
    head = r.git("rev-parse", "HEAD").strip()
    api = Api("0" * 40, head, "x")
    rc = sc.post_status_run("o/r", base="main~1", head="main", client=api, out=lambda s: None)
    assert rc == 0 and api.posts[0][0] == f"/repos/o/r/statuses/{head}"
    assert "target_url" not in api.status()


@pytest.mark.parametrize("argv", [
    ["--pr", "1"], ["--override", "a good reason here"],
    ["--post-status", "o/r", "--override", "a good reason here"],
    ["--post-status", "o/r", "--pr", "1", "main", "HEAD"],
    ["--post-status", "o/r", "--pr", "1", "--branch", "x"],
    ["main"], [],
])
def test_cli_rejects_inconsistent_arguments(argv, capsys):
    with pytest.raises(SystemExit) as e:
        sc.main(argv)
    assert e.value.code == 2


def test_invalid_repo_is_refused_before_anything_runs():
    with pytest.raises(ValueError):
        sc.post_status_run("../etc", base="a", head="b", client=object())


# --- the ruleset that requires the status ------------------------------------- #
def test_shipped_ruleset_requires_exactly_the_status_context(repo_root):
    import os
    import sandbox_protect as sp
    path = os.path.join(repo_root, "rulesets", "require-sandbox-check.json")
    rs = sp.validate_repo_ruleset(sp.load_ruleset(path))   # same allowlist as any repo's
    assert sp.required_checks(rs) == [sc.STATUS_CONTEXT]
    assert rs["bypass_actors"] == [] and rs["enforcement"] == "active"
    (rule,) = rs["rules"]
    assert rule["parameters"]["strict_required_status_checks_policy"] is True
    assert json.load(open(path))["name"] == "require-sandbox-check"


# --- gating a session's PRs (plan 5d) ---------------------------------------- #
def landed(*pairs):
    from sandbox_land import Landed     # the real type: a renamed field must fail here
    return [Landed("b", "main", o, u, 1) for o, u in pairs]


class RulesApi:
    def __init__(self, required=True, ctx="sandbox/check"):
        self.required, self.ctx, self.calls = required, ctx, []

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append(path)
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if path == "/repos/o/r/rules/branches/main":
            return [{"type": "pull_request"},
                    {"type": "required_status_checks", "parameters": {
                        "required_status_checks": [{"context": self.ctx}]}}
                    ] if self.required else [{"type": "pull_request"}]
        raise AssertionError(path)


def test_gate_required_detects_the_context_in_the_effective_rules():
    assert sc.gate_required(RulesApi(), "o/r") is True
    assert sc.gate_required(RulesApi(required=False), "o/r") is False
    assert sc.gate_required(RulesApi(ctx="commit-metadata"), "o/r") is False


def test_pr_numbers_only_from_opened_or_existing_prs():
    got = sc.pr_numbers(landed(("opened", "https://github.com/o/r/pull/7"),
                               ("exists", "https://github.com/o/r/pull/8/"),
                               ("held", ""), ("empty", ""),
                               ("opened", "not-a-pr-url")))
    assert got == [7, 8]


def test_statuses_are_posted_only_where_the_repo_requires_them():
    calls = []
    poster = lambda repo, pr, **k: calls.append(pr)
    none = sc.post_session_statuses("o/r", landed(("opened", "x/pull/7")),
                                    api=RulesApi(required=False), poster=poster)
    assert none == [] and calls == []
    out = sc.post_session_statuses("o/r", landed(("opened", "x/pull/7")),
                                   api=RulesApi(), poster=poster)
    assert calls == [7] and [o.pr for o in out] == [7]


def test_no_prs_means_no_api_calls_at_all():
    api = RulesApi()
    assert sc.post_session_statuses("o/r", landed(("held", "")), api=api) == []
    assert api.calls == []


def test_one_prs_failure_is_reported_and_does_not_stop_the_others():
    def poster(repo, pr, result, **k):
        if pr == 7:
            raise sc.CheckError("PR #7 comes from a fork")
        result.update(state="success", description="0 errors, 0 warnings")
    out = sc.post_session_statuses(
        "o/r", landed(("opened", "x/pull/7"), ("opened", "x/pull/8")),
        api=RulesApi(), poster=poster)
    assert [(o.pr, o.state) for o in out] == [(7, "failed"), (8, "success")]
    text = sc.format_status_outcomes(out)
    assert "#7: failed (PR #7 comes from a fork)" in text and "--pr 7" in text
    assert "#8: success" in text and "--pr 8" not in text
    assert sc.format_status_outcomes([]) == ""


# --- the check-failed label (plan 4b) ------------------------------------------- #
def labelled(api):
    return [b["labels"] for p, b in api.label_calls if "/issues/" in p]


def test_a_red_status_adds_check_failed(pr):
    api, url, _ = pr(["fixup! x"])
    go(api, url)
    assert labelled(api) == [["check-failed"]]
    assert api.deletes == []


def test_a_green_status_removes_check_failed(pr):
    api, url, _ = pr(["feat: ok"])
    go(api, url)
    assert api.deletes == [("/repos/o/r/issues/1/labels/check-failed", None)]
    assert labelled(api) == []


def test_override_turns_the_label_off(pr):
    api, url, _ = pr(["fixup! x"])
    go(api, url, override="reviewed by hand, fine")
    assert api.deletes and labelled(api) == []


def test_a_crashed_checker_is_labelled_failed_too(pr, monkeypatch):
    api, url, _ = pr(["feat: x"])
    monkeypatch.setattr(sc, "check_findings", lambda *a, **k: 1 / 0)
    go(api, url)
    assert labelled(api) == [["check-failed"]]


def test_label_trouble_never_changes_the_status_or_exit_code(pr):
    api, url, _ = pr(["fixup! x"])
    api.fail_labels = True
    rc, _ = go(api, url)
    assert rc == 1 and api.status()["state"] == "failure"


def test_local_range_mode_touches_no_labels(tmp_path, monkeypatch):
    r = Repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    r.commit("feat: a", {"a.txt": "1"})
    api = Api("0" * 40, r.git("rev-parse", "HEAD").strip(), "x")
    sc.post_status_run("o/r", base="main~1", head="main", client=api, out=lambda s: None)
    assert api.label_calls == [] and api.deletes == []
