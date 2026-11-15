"""Tests for sandbox_land: sensitive-path holds, untrusted text, PR flow."""
import pytest

import sandbox_land as sl


def test_sensitive_paths():
    files = ["README.md", ".github/workflows/tests.yml", ".GitHub/x",
             "claude_sandbox_deps.yaml", "claude_sandbox_host.yaml",
             ".githooks/pre-push", "docs/CODEOWNERS", "tools/setup.sh",
             "src/github/ok.py"]
    assert sl.sensitive_paths(files, extra=["tools/setup.sh"]) == sorted(
        [".GitHub/x", ".github/workflows/tests.yml", ".githooks/pre-push",
         "claude_sandbox_deps.yaml", "claude_sandbox_host.yaml",
         "docs/CODEOWNERS", "tools/setup.sh"])
    assert sl.sensitive_paths(["a.py", "tools/setup.sh"]) == []


def test_clean_text_neutralises_mentions_fences_and_control():
    t = sl.clean_text("fix @octocat `rm`\n\x1b[31m```\nx", 200)
    assert "@octocat" not in t and "`" not in t and "\n" not in t
    assert "\x1b" not in t
    assert len(sl.clean_text("a" * 500, 50)) == 50


def test_pr_text_is_labelled_untrusted_and_bounded():
    subjects = [f"commit {i} @bob" for i in range(30)]
    title, body = sl.pr_text("quirky-hopper", "sbx/quirky-hopper/work", subjects)
    assert title.startswith("[quirky-hopper] work (30 commits)")
    assert "untrusted" in body and "@bob" not in body
    assert "10 more commits" in body
    one, _ = sl.pr_text("s", "sbx/s/work", ["only @x"])
    assert "@x" not in one and "only" in one


class Api:
    """Fake GitHub. compares: {(base, head): compare-dict}; unknown pairs are
    'ahead by 1' so simple tests need only the default-branch compare."""

    def __init__(self, compares, pulls=(), branches=("sbx/s/work",)):
        self.compares, self.pulls, self.posted = compares, list(pulls), []
        self.branches = list(branches)
        self.label_calls, self.fail_labels = [], False
        self.head_sha, self.patched = TIP, []

    def __call__(self, method, path, body=None, raw=False):
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if "/git/matching-refs/" in path:
            return [{"ref": f"refs/heads/{b}"} for b in self.branches]
        if "/compare/" in path:
            base, head = path.split("/compare/")[1].split("...")
            return self.compares.get((base, head), cmp_())
        if "/pulls?" in path:
            return self.pulls
        if method == "POST" and path.endswith("/labels"):
            if self.fail_labels:
                raise RuntimeError("403")
            self.label_calls.append((path, body))
            return {}
        if method == "POST":
            self.posted.append(body)
            n = len(self.posted)
            return {"html_url": f"https://github.com/o/r/pull/{n}", "number": n,
                    "head": {"sha": self.head_sha}}
        if method == "PATCH":
            self.patched.append((path, body))
            return {}
        raise AssertionError(path)


TIP = "a" * 40


def cmp_(files=("a.py",), ahead=1, status="ahead"):
    return {"ahead_by": ahead, "status": status, "total_commits": ahead,
            "files": [{"filename": f} for f in files],
            "commits": [{"sha": TIP, "commit": {"message": "do it\n\nbody"}}]
            * ahead}


def main_cmp(c, head="sbx/s/work"):
    return {("main", head): c}


def test_session_branches_filters_to_exact_prefix(monkeypatch):
    monkeypatch.setattr(sl, "gh_api", Api({}, branches=["sbx/s/work",
                                                         "sbx/sx/other"]))
    assert sl.session_branches("o/r", "s") == ["sbx/s/work"]


def test_land_opens_pr_with_safe_fields(monkeypatch):
    api = Api({})
    monkeypatch.setattr(sl, "gh_api", api)
    (r,) = sl.land("o/r", "s")
    assert r == sl.Landed("sbx/s/work", "main", "opened",
                          "https://github.com/o/r/pull/1", 1)
    (pr,) = api.posted
    assert pr["head"] == "sbx/s/work" and pr["base"] == "main"
    assert pr["maintainer_can_modify"] is False and pr["title"] == "[s] do it"


@pytest.mark.parametrize("compare,pulls,outcome", [
    (cmp_(ahead=0), [], "empty"),
    (cmp_(files=[".github/workflows/x.yml"]), [], "held"),
    (cmp_(files=["f"] * 300), [], "held"),
    (cmp_(), [{"html_url": "u", "base": {"ref": "main"}}], "exists"),
])
def test_land_outcomes_never_open_when_not_due(monkeypatch, compare, pulls,
                                               outcome):
    api = Api(main_cmp(compare), pulls)
    monkeypatch.setattr(sl, "gh_api", api)
    assert sl.land("o/r", "s")[0].outcome == outcome
    assert api.posted == []


def test_land_holds_setup_scripts(monkeypatch):
    monkeypatch.setattr(sl, "gh_api", Api(main_cmp(cmp_(files=["t/setup.sh"]))))
    assert sl.land("o/r", "s", ["t/setup.sh"])[0].outcome == "held"


# -- stacking -------------------------------------------------------------- #
def test_choose_base_picks_closest_strict_ancestor():
    assert sl.choose_base("main", []) == "main"
    assert sl.choose_base("main", [("sbx/s/a", "ahead", 5),
                                   ("sbx/s/b", "ahead", 2)]) == "sbx/s/b"
    # identical, behind or diverged branches are never a parent
    assert sl.choose_base("main", [("x", "identical", 0), ("y", "behind", 0),
                                   ("z", "diverged", 3)]) == "main"


def test_stacked_branch_targets_its_parent_with_incremental_diff(monkeypatch):
    br = ["sbx/s/1-api", "sbx/s/2-tests"]
    api = Api({("main", "sbx/s/1-api"): cmp_(ahead=2),
               ("main", "sbx/s/2-tests"): cmp_(ahead=3),
               ("sbx/s/1-api", "sbx/s/2-tests"): cmp_(ahead=1),
               ("sbx/s/2-tests", "sbx/s/1-api"): cmp_(ahead=0, status="behind")},
              branches=br)
    monkeypatch.setattr(sl, "gh_api", api)
    parent, child = sl.land("o/r", "s")
    assert (parent.base, child.base) == ("main", "sbx/s/1-api")
    assert child.commits == 1  # only the increment
    assert "Stacked on `sbx/s/1-api`" in api.posted[1]["body"]
    assert "Stacked" not in api.posted[0]["body"]


def test_never_stacks_on_another_sessions_branch(monkeypatch):
    api = Api({("sbx/o/x", "sbx/s/work"): cmp_(ahead=1)},
              branches=["sbx/s/work", "sbx/o/x"])
    monkeypatch.setattr(sl, "gh_api", api)
    mine = [r for r in sl.land("o/r") if r.branch == "sbx/s/work"][0]
    assert mine.base == "main"


def test_branch_above_a_held_one_is_held_too(monkeypatch):
    # the child's cumulative diff vs the default branch includes the workflow
    api = Api({("main", "sbx/s/1"): cmp_(files=[".github/workflows/a.yml"]),
               ("main", "sbx/s/2"): cmp_(files=[".github/workflows/a.yml", "b"])},
              branches=["sbx/s/1", "sbx/s/2"])
    monkeypatch.setattr(sl, "gh_api", api)
    assert [r.outcome for r in sl.land("o/r", "s")] == ["held", "held"]
    assert api.posted == []


# -- exit summary ---------------------------------------------------------- #
def test_format_landing_summary():
    L = sl.Landed
    out = sl.format_landing([
        L("sbx/q/api", "main", "opened", "https://github.com/o/r/pull/12", 3),
        L("sbx/q/api-tests", "sbx/q/api", "opened",
          "https://github.com/o/r/pull/13", 1),
        L("sbx/q/ci", "main", "held", ".github/workflows/x.yml", 2),
        L("sbx/q/x", "main", "empty", "", 0)], "q")
    assert "pull requests for session q" in out
    assert "#12" in out and "#13" in out and "[3 commits]" in out
    assert "[1 commit]" in out and "(stacked)" in out
    assert "HELD" in out and ".github/workflows/x.yml" in out
    assert "2 opened" in out and "bottom-up" in out and "never merges" in out
    assert "\033" not in out  # no color unless asked
    assert "\033[" in sl.format_landing([L("b", "main", "empty", "", 0)],
                                         color=True)


def test_format_landing_with_nothing_pushed():
    assert "no session branches were pushed" in sl.format_landing([])
    assert "bottom-up" not in sl.format_landing([])


# -- labels (plan 4b) ---------------------------------------------------------- #
def test_opened_prs_are_labelled_sandbox_and_with_their_session(monkeypatch):
    api = Api({})
    monkeypatch.setattr(sl, "gh_api", api)
    sl.land("o/r", "s")
    created = [b["name"] for p, b in api.label_calls if p == "/repos/o/r/labels"]
    assert created == ["sandbox", "session:s"]
    (path, body), = [c for c in api.label_calls if "/issues/" in c[0]]
    assert path == "/repos/o/r/issues/1/labels"
    assert body == {"labels": ["sandbox", "session:s"]}


def test_a_refused_label_never_breaks_landing(monkeypatch):
    api = Api({})
    api.fail_labels = True
    monkeypatch.setattr(sl, "gh_api", api)
    (r,) = sl.land("o/r", "s")
    assert r.outcome == "opened" and len(api.posted) == 1


@pytest.mark.parametrize("compare,pulls", [
    (cmp_(ahead=0), []), (cmp_(files=[".github/workflows/x.yml"]), []),
    (cmp_(), [{"html_url": "u", "base": {"ref": "main"}}]),
])
def test_only_prs_we_open_are_labelled(monkeypatch, compare, pulls):
    api = Api(main_cmp(compare), pulls)
    monkeypatch.setattr(sl, "gh_api", api)
    sl.land("o/r", "s")
    assert api.label_calls == []


def test_pr_number_falls_back_to_the_url_and_unlabelable_prs_are_skipped(monkeypatch):
    api = Api({})
    monkeypatch.setattr(sl, "gh_api", api)
    sl.label_pr("o/r", {"html_url": "https://github.com/o/r/pull/42"}, "s")
    assert any(p == "/repos/o/r/issues/42/labels" for p, _ in api.label_calls)
    api.label_calls.clear()
    sl.label_pr("o/r", {"html_url": "nonsense"}, "s")
    assert api.label_calls == []
    sl.label_pr("o/r", {"number": 5}, "bad session!")      # only the generic label
    assert api.label_calls[-1][1] == {"labels": ["sandbox"]}


def test_a_branch_that_moves_while_landing_gets_its_pr_closed(monkeypatch):
    """The PR's head is a branch name: if another key pushes between the hold
    check and the POST, the PR would merge something nobody checked."""
    api = Api({})
    api.head_sha = "b" * 40
    monkeypatch.setattr(sl, "gh_api", api)
    (r,) = sl.land("o/r", "s")
    assert r.outcome == "held" and "moved while landing" in r.detail
    assert "closed" in r.detail
    assert api.patched == [("/repos/o/r/pulls/1", {"state": "closed"})]
    assert api.label_calls == []


def test_a_pr_response_without_a_head_sha_fails_closed(monkeypatch):
    api = Api({})
    api.head_sha = None
    monkeypatch.setattr(sl, "gh_api", api)
    assert sl.land("o/r", "s")[0].outcome == "held"


def test_a_compare_that_doesnt_list_every_commit_is_held_unopened(monkeypatch):
    c = cmp_()
    c["total_commits"] = 251
    api = Api(main_cmp(c))
    monkeypatch.setattr(sl, "gh_api", api)
    (r,) = sl.land("o/r", "s")
    assert r.outcome == "held" and "pin" in r.detail
    assert api.posted == []


def test_checked_tip_is_the_last_listed_commit():
    c = {"total_commits": 2, "commits": [{"sha": "1" * 40}, {"sha": "2" * 40}]}
    assert sl.checked_tip(c) == "2" * 40
    assert sl.checked_tip({"commits": [{"sha": "nope"}]}) is None
    assert sl.checked_tip({}) is None
