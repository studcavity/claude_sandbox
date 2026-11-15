"""Tests for land: sensitive-path holds, untrusted text, PR flow."""
import threading

import pytest

from claude_sandbox.github import land as sl
from claude_sandbox.github.gh import GitHubError


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

    def __init__(self, compares, pulls=(), branches=("sbx/s/work",), deps=None):
        self.compares, self.pulls, self.posted = compares, list(pulls), []
        self.deps = deps  # raw claude_sandbox_deps.yaml on the default branch
        self.branches = list(branches)
        self.label_calls, self.fail_labels = [], False
        self.head_sha, self.patched = TIP, []

    def __call__(self, method, path, body=None, raw=False):
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if "/contents/claude_sandbox_deps.yaml" in path:
            assert raw and path.endswith("?ref=main")
            if self.deps is None:
                raise GitHubError("not found", 404)
            return self.deps
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


SETUP_DEPS = b"setup:\n  - name: prep\n    path: t/setup.sh\n"


def test_land_holds_setup_scripts_named_in_the_default_branchs_deps(monkeypatch):
    """The hold comes from the deps file on the default branch, read at
    landing time, so it doesn't depend on which launcher path landed (a
    --land-all or --no-deps run never parses the deps file)."""
    api = Api(main_cmp(cmp_(files=["t/setup.sh"])), deps=SETUP_DEPS)
    monkeypatch.setattr(sl, "gh_api", api)
    assert sl.land("o/r", "s")[0].outcome == "held"
    assert api.posted == []
    assert sl.land("o/r")[0].outcome == "held"  # session=None, as --land-all


def test_setup_script_paths_tolerates_missing_or_broken_deps(monkeypatch):
    monkeypatch.setattr(sl, "gh_api", Api({}))
    assert sl.setup_script_paths("o/r", "main") == []
    monkeypatch.setattr(sl, "gh_api", Api({}, deps=b"setup: [not a list of maps"))
    assert sl.setup_script_paths("o/r", "main") == []
    monkeypatch.setattr(sl, "gh_api", Api({}, deps=SETUP_DEPS))
    assert sl.setup_script_paths("o/r", "main") == ["t/setup.sh"]


def test_setup_script_paths_surfaces_other_github_errors(monkeypatch):
    def boom(*a, **kw):
        raise GitHubError("rate limited", 403)
    monkeypatch.setattr(sl, "gh_api", boom)
    with pytest.raises(GitHubError):
        sl.setup_script_paths("o/r", "main")


def test_confirmed_hold_opens_the_pr(monkeypatch):
    api = Api(main_cmp(cmp_(files=[".github/workflows/x.yml"])))
    monkeypatch.setattr(sl, "gh_api", api)
    asked = []
    (r,) = sl.land("o/r", "s", confirm=lambda b, d: asked.append((b, d)) or True)
    assert asked == [("sbx/s/work", ".github/workflows/x.yml")]
    assert r.outcome == "opened" and len(api.posted) == 1


def test_declined_hold_stays_held(monkeypatch):
    api = Api(main_cmp(cmp_(files=[".github/workflows/x.yml"])))
    monkeypatch.setattr(sl, "gh_api", api)
    (r,) = sl.land("o/r", "s", confirm=lambda b, d: False)
    assert r.outcome == "held" and api.posted == []


def test_confirm_is_not_asked_for_clean_branches(monkeypatch):
    monkeypatch.setattr(sl, "gh_api", Api({}))
    sl.land("o/r", "s", confirm=lambda b, d: pytest.fail("asked"))


def test_unpinnable_tip_is_never_overridable(monkeypatch):
    c = cmp_(files=[".github/x.yml"])
    c["total_commits"] = 300  # more than the compare lists
    api = Api(main_cmp(c))
    monkeypatch.setattr(sl, "gh_api", api)
    (r,) = sl.land("o/r", "s", confirm=lambda b, d: True)
    assert r.outcome == "held" and api.posted == []


def test_confirmed_branch_that_moves_is_still_closed(monkeypatch):
    api = Api(main_cmp(cmp_(files=[".github/x.yml"])))
    api.head_sha = "b" * 40
    monkeypatch.setattr(sl, "gh_api", api)
    (r,) = sl.land("o/r", "s", confirm=lambda b, d: True)
    assert r.outcome == "held" and api.patched


# -- every PR targets the default branch -------------------------------------- #
def test_branches_built_on_each_other_all_target_the_default_branch(monkeypatch):
    """Spec 05: a PR into another session branch would get no CI (the
    workflows are filtered to the default branch), so none is ever opened, and
    landing costs a fixed number of GitHub calls per branch."""
    br = ["sbx/s/1-api", "sbx/s/2-tests"]
    api = Api({("main", "sbx/s/1-api"): cmp_(ahead=2),
               ("main", "sbx/s/2-tests"): cmp_(ahead=3)}, branches=br)
    calls = []
    inner = api.__call__
    monkeypatch.setattr(sl, "gh_api",
                        lambda m, p, *a, **kw: calls.append(p) or inner(m, p, *a, **kw))
    parent, child = sl.land("o/r", "s")
    assert (parent.base, child.base) == ("main", "main")
    assert (parent.commits, child.commits) == (2, 3)  # cumulative diff
    assert [p["base"] for p in api.posted] == ["main", "main"]
    assert all("Stacked" not in p["body"] for p in api.posted)
    assert not any("/compare/sbx/" in p for p in calls)  # no sibling compares


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
        L("sbx/q/api-tests", "main", "opened",
          "https://github.com/o/r/pull/13", 1),
        L("sbx/q/ci", "main", "held", ".github/workflows/x.yml", 2),
        L("sbx/q/x", "main", "empty", "", 0)], "q")
    assert "pull requests for session q" in out
    assert "#12" in out and "#13" in out and "[3 commits]" in out
    assert "[1 commit]" in out and "stacked" not in out
    assert "HELD" in out and ".github/workflows/x.yml" in out
    assert "2 opened" in out and "never merges" in out
    assert "\033" not in out  # no color unless asked
    assert "\033[" in sl.format_landing([L("b", "main", "empty", "", 0)],
                                         color=True)


def test_format_landing_with_nothing_pushed():
    assert "no session branches were pushed" in sl.format_landing([])


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


# -- the PR body's "not verified" section ------------------------------------ #
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2    # assembled: nothing token-shaped in source


def test_only_not_verified_lines_are_lifted_with_their_commit():
    got = sl.not_verified_lines([
        ("aaaaaaa", "feat: x\n\nVerified: pyflakes\nNot verified against real incus\n"
                    "Follow-up: run it on a host"),
        ("bbbbbbb", "fix: y\n\nVerified inside a sandbox"),
        ("ccccccc", "fix: z\n\n  not verified: lower case, indented")])
    assert got == [("aaaaaaa", "Not verified against real incus"),
                   ("ccccccc", "not verified: lower case, indented")]


@pytest.mark.parametrize("line", [
    "we never verified this", "unverified", "Verifiedish", "see: Not verified: x",
    "Verified: fine", "Follow-up: not verified yet",
])
def test_only_lines_that_start_with_the_words_count(line):
    assert sl.not_verified_lines([("aaaaaaa", f"feat: x\n\n{line}")]) == []


def test_a_hostile_message_is_only_read_so_far():
    late = "\n".join(["filler"] * sl.MAX_LINES_PER_MESSAGE + ["Not verified: too late"])
    assert sl.not_verified_lines([("aaaaaaa", late)]) == []


def body(items, subjects=("feat: one",)):
    return sl.pr_text("sess", "sbx/sess/work", list(subjects), items)[1]


def test_the_section_comes_before_the_subjects_and_is_labelled_untrusted():
    b = body([("aaaaaaa", "Not verified: no incus")])
    assert "### Not verified, according to the commits" in b
    assert "`aaaaaaa` Not verified: no incus" in b and "(1)" in b
    assert b.index("Not verified, according") < b.index("````")
    assert "untrusted" in b


def test_the_section_says_so_when_no_commit_has_a_caveat():
    assert "No commit says anything is not verified" in body([])


def test_long_lists_are_truncated_with_a_count():
    items = [(f"{i:07x}", f"Not verified: item {i}") for i in range(15)]
    b = body(items)
    assert "(15)" in b and "...and 5 more" in b
    assert "item 9" in b and "item 10" not in b


def test_session_text_is_sanitised_in_the_section():
    b = body([("aaaaaaa", "Not verified: ping @octocat `rm -rf` \x1b[31m")])
    assert "@octocat" not in b and "`rm -rf`" not in b and "\x1b" not in b


def test_a_credential_in_a_commit_message_is_withheld_not_copied_into_the_pr():
    b = body([("aaaaaaa", f"Not verified: used {GH}")], subjects=[f"feat: add {GH}"])
    assert GH not in b
    assert b.count("[withheld: appears to contain GitHub token]") == 2


def test_a_forged_sha_cannot_break_out_of_its_code_span():
    b = body([("a`b`<script>", "Not verified: x")])
    assert "<script>" not in b and "`?`" in b


def test_without_caveats_the_body_has_no_section():
    t, b = sl.pr_text("sess", "sbx/sess/work", ["feat: one"])
    assert "Not verified" not in b and t == "[sess] feat: one"


def test_land_branch_puts_the_commits_caveats_in_the_pr(monkeypatch):
    c = cmp_()
    c["commits"][0]["commit"]["message"] = "do it\n\nNot verified: needs a host"
    api = Api(main_cmp(c))
    monkeypatch.setattr(sl, "gh_api", api)
    sl.land("o/r", "s")
    assert "Not verified: needs a host" in api.posted[0]["body"]


# --- proposed synopsis ------------------------------------------------------------ #
SYN = "feat: x\n\nPR-Title: Add the thing\nPR-Summary: does a\nPR-Summary: because b\n"


def syn_body(syn):
    return sl.pr_text("sess", "sbx/sess/work", ["feat: one"], [], syn)[1]


def test_synopsis_comes_from_the_newest_commit_that_has_a_title():
    r = sl.synopsis([("aaaaaaa", SYN), ("bbbbbbb", "PR-Title: Better title\nPR-Summary: new"),
                     ("ccccccc", "fix: z\n\nPR-Summary: orphan summary, no title")])
    assert r == ("bbbbbbb", "Better title", ["new"])
    assert sl.synopsis([("aaaaaaa", "feat: x")]) is None


def test_body_shows_the_synopsis_as_untrusted_and_sanitised():
    syn = sl.synopsis([("aaaaaaa", "feat: x\n\nPR-Title: ping @octocat `rm` fixes #12\n"
                                   f"PR-Summary: leaked {GH}\nPR-Summary: ok\x1b[31m")])
    b = syn_body(syn)
    assert "### Proposed synopsis" in b and "untrusted" in b
    assert b.index("Proposed synopsis") < b.index("Not verified, according")
    assert "@octocat" not in b and "`rm`" not in b and "#12" not in b.replace("#​12", "")
    assert GH not in b and "[withheld: appears to contain GitHub token]" in b
    assert "\x1b" not in b
    t, _ = sl.pr_text("sess", "sbx/sess/work", ["feat: one"], [], syn)
    assert t.endswith(" [sess]") and "@octocat" not in t and "`" not in t
    assert t.startswith("ping @​octocat")


def test_no_synopsis_section_without_a_synopsis():
    assert "Proposed synopsis" not in syn_body(None)


def test_title_is_the_proposed_one_with_the_session_appended():
    syn = sl.synopsis([("aaaaaaa", SYN)])
    t, _ = sl.pr_text("sess", "sbx/sess/work", ["feat: one", "feat: two"], [], syn)
    assert t == "Add the thing [sess]"
    t, _ = sl.pr_text("sess", "sbx/sess/work", ["feat: one"], [], None)
    assert t == "[sess] feat: one"          # no proposal: built from the subjects


def test_land_reads_run_concurrently_but_writes_stay_on_this_thread(monkeypatch):
    """The compare and open-PR reads of every branch overlap (a barrier only
    releases when all four are in flight at once); the PR POSTs and the user
    prompt stay on the calling thread, in branch order."""
    branches = ["sbx/s/a", "sbx/s/b"]
    api = Api({}, branches=branches)
    barrier = threading.Barrier(2 * len(branches), timeout=5)
    main = threading.current_thread()
    writers, prompts = [], []

    def gated(method, path, body=None, raw=False):
        if "/compare/" in path or "/pulls?" in path:
            barrier.wait()   # raises BrokenBarrierError if reads were serial
        elif method == "POST":
            writers.append(threading.current_thread())
        return api(method, path, body, raw)
    monkeypatch.setattr(sl, "gh_api", gated)
    results = sl.land("o/r", "s", confirm=lambda b, d: prompts.append(
        threading.current_thread()))
    assert [r.branch for r in results] == branches
    assert [p["head"] for p in api.posted] == branches
    assert writers and all(t is main for t in writers)


def test_land_skips_the_deps_read_when_there_is_nothing_to_land(monkeypatch):
    seen = []
    api = Api({}, branches=[])
    monkeypatch.setattr(sl, "gh_api", lambda m, p, *a, **k: (
        seen.append(p), api(m, p, *a, **k))[1])
    assert sl.land("o/r", "s") == []
    assert not any("contents" in p for p in seen)
