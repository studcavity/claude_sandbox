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
    def __init__(self, compare, pulls=()):
        self.compare, self.pulls, self.posted = compare, list(pulls), []

    def __call__(self, method, path, body=None, raw=False):
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if "/git/matching-refs/" in path:
            return [{"ref": "refs/heads/sbx/s/work"},
                    {"ref": "refs/heads/sbx/sx/other"}]
        if "/compare/" in path:
            return self.compare
        if "/pulls?" in path:
            return self.pulls
        if method == "POST":
            self.posted.append(body)
            return {"html_url": "https://github.com/o/r/pull/1"}
        raise AssertionError(path)


def cmp_(files=("a.py",), ahead=1):
    return {"ahead_by": ahead, "files": [{"filename": f} for f in files],
            "commits": [{"commit": {"message": "do it\n\nbody"}}] * ahead}


def test_session_branches_filters_to_exact_prefix(monkeypatch):
    monkeypatch.setattr(sl, "gh_api", Api(cmp_()))
    assert sl.session_branches("o/r", "s") == ["sbx/s/work"]


def test_land_opens_pr_with_safe_fields(monkeypatch):
    api = Api(cmp_())
    monkeypatch.setattr(sl, "gh_api", api)
    assert sl.land("o/r", "s") == [
        ("sbx/s/work", "opened", "https://github.com/o/r/pull/1")]
    (pr,) = api.posted
    assert pr["head"] == "sbx/s/work" and pr["base"] == "main"
    assert pr["maintainer_can_modify"] is False and pr["title"] == "[s] do it"


@pytest.mark.parametrize("compare,pulls,outcome", [
    (cmp_(ahead=0), [], "empty"),
    (cmp_(files=[".github/workflows/x.yml"]), [], "held"),
    (cmp_(files=["f"] * 300), [], "held"),
    (cmp_(), [{"html_url": "u"}], "exists"),
])
def test_land_outcomes_never_open_when_not_due(monkeypatch, compare, pulls,
                                               outcome):
    api = Api(compare, pulls)
    monkeypatch.setattr(sl, "gh_api", api)
    assert sl.land("o/r", "s")[0][1] == outcome
    assert api.posted == []


def test_land_holds_setup_scripts(monkeypatch):
    monkeypatch.setattr(sl, "gh_api", Api(cmp_(files=["tools/setup.sh"])))
    assert sl.land("o/r", "s", ["tools/setup.sh"])[0][1] == "held"
