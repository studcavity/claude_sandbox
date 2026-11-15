"""Tests for sandbox_github: repo resolution and the gh-CLI-via-invoke client."""
import json
import shlex

import pytest

from claude_sandbox.github import gh as g
@pytest.mark.parametrize("repo", ["me/proj", "my-org/my.repo_1", "a/b"])
def test_validate_repo_accepts(repo):
    assert g.validate_repo(repo) == repo


@pytest.mark.parametrize("repo", ["proj", "a/b/c", "a/b;rm -rf", "-x/y", "a/..",
                                  "a/b.git", "a/ b", ""])
def test_validate_repo_rejects(repo):
    with pytest.raises(ValueError):
        g.validate_repo(repo)


def test_resolve_repo():
    assert g.resolve_repo("proj", "me") == "me/proj"
    assert g.resolve_repo("org/proj", "me") == "org/proj"
    for bad in ("..", ".", "a b", "x;y", "a/b/c", "a/.."):
        with pytest.raises(ValueError):
            g.resolve_repo(bad, "me")


class Result:
    def __init__(self, stdout="", stderr="", exited=0):
        self.stdout, self.stderr, self.exited = stdout, stderr, exited

    @property
    def failed(self):
        return self.exited != 0


@pytest.fixture
def gh(monkeypatch):
    """Replace invoke's Context with one that records commands and replies
    with whatever the test queues."""
    calls, replies = [], []

    class FakeContext:
        def run(self, command, **kw):
            stdin = kw["in_stream"]
            calls.append({"argv": shlex.split(command), "kw": kw,
                          "stdin": stdin.read() if stdin else stdin})
            return replies.pop(0)
    monkeypatch.setattr(g, "Context", FakeContext)
    return calls, replies


def test_gh_api_builds_argv_and_sends_body_on_stdin(gh):
    calls, replies = gh
    replies.append(Result('{"id": 7}'))
    assert g.gh_api("POST", "/user/keys", {"key": "k; rm -rf ~"}) == {"id": 7}
    c = calls[0]
    assert c["argv"] == ["gh", "api", "-X", "POST", "/user/keys",
                         "--input", "-"]
    assert json.loads(c["stdin"]) == {"key": "k; rm -rf ~"}  # data, not argv
    assert c["kw"]["hide"] and c["kw"]["warn"] and c["kw"]["timeout"] == g.TIMEOUT
    assert c["kw"]["env"]["GH_PROMPT_DISABLED"] == "1"


def test_gh_api_quotes_hostile_paths(gh):
    calls, replies = gh
    replies.append(Result("{}"))
    g.gh_api("GET", "/repos/a/b; touch /tmp/x")
    assert calls[0]["argv"][4] == "/repos/a/b; touch /tmp/x"  # one argv item
    assert calls[0]["stdin"] is False  # never inherit the terminal's stdin


def test_gh_api_raw_and_empty(gh):
    calls, replies = gh
    replies += [Result("raw bytes"), Result("")]
    assert g.gh_api("GET", "/x", raw=True) == b"raw bytes"
    assert "Accept: application/vnd.github.raw+json" in calls[0]["argv"]
    assert g.gh_api("DELETE", "/x") is None


def test_gh_api_http_error_carries_code(gh):
    _, replies = gh
    replies.append(Result(stdout='{"message":"Not Found"}',
                          stderr="gh: Not Found (HTTP 404)", exited=1))
    with pytest.raises(g.GitHubError) as e:
        g.gh_api("GET", "/missing")
    assert e.value.code == 404 and "Not Found" in str(e.value)


def test_gh_missing_is_a_clear_error(gh):
    _, replies = gh
    replies.append(Result(stderr="bash: gh: command not found", exited=127))
    with pytest.raises(g.GitHubError, match="not installed"):
        g.github_login()


def test_not_logged_in_hint_does_not_depend_on_gh_wording(gh):
    """Under GitHub Actions gh words this differently; exit status 4 is what
    we key on."""
    _, replies = gh
    replies.append(Result(stderr="gh: To use GitHub CLI in a GitHub Actions "
                                 "workflow, set the GH_TOKEN environment "
                                 "variable.", exited=4))
    with pytest.raises(g.GitHubError, match="gh auth login") as e:
        g.github_login()
    assert "GH_TOKEN" in str(e.value) and e.value.code == 0


def test_not_logged_in_error_is_surfaced(gh):
    _, replies = gh
    replies.append(Result(stderr="To get started with GitHub CLI, please run:  "
                                 "gh auth login", exited=4))
    with pytest.raises(g.GitHubError, match="gh auth login") as e:
        g.github_login()
    assert e.value.code == 0


def test_timeout_becomes_github_error(monkeypatch):
    class Slow:
        def run(self, *a, **k):
            raise g.CommandTimedOut(None, 60)
    monkeypatch.setattr(g, "Context", Slow)
    with pytest.raises(g.GitHubError, match="timed out"):
        g.gh_api("GET", "/x")


def test_github_login(gh):
    calls, replies = gh
    replies.append(Result("studcavity\n"))
    assert g.github_login() == "studcavity"
    assert calls[0]["argv"] == ["gh", "api", "user", "--jq", ".login"]


@pytest.mark.parametrize("headers, expect", [
    ("X-Oauth-Scopes: repo, workflow, admin:public_key",
     {"repo", "workflow", "admin:public_key"}),
    ("x-oauth-scopes: ", set()),
    ("Content-Type: application/json", None),
])
def test_token_scopes(gh, headers, expect):
    _, replies = gh
    replies.append(Result(f"HTTP/2.0 200 OK\n{headers}\n\n"
                          '{"X-Oauth-Scopes": "from the body, not a header"}'))
    assert g.token_scopes() == expect


def test_token_scopes_tolerates_crlf_header_blocks(gh):
    _, replies = gh
    replies.append(Result("HTTP/2.0 200 OK\r\nX-Oauth-Scopes: repo\r\n\r\n{}"))
    assert g.token_scopes() == {"repo"}


def test_workflow_push_warning_fires_when_the_sandbox_could_push_workflows():
    """The scope lets a session push a workflow that runs with Actions
    secrets on push, before any PR exists; lacking it is the safe default."""
    assert g.workflow_push_warning({"repo"}) is None
    has = g.workflow_push_warning({"repo", "workflow"})
    assert "has the `workflow` scope" in has and "secrets" in has
    assert "--remove-scopes workflow" in has
    assert "-s workflow" not in has   # never advise granting it
    unknown = g.workflow_push_warning(None)
    assert "couldn't verify" in unknown and "secrets" in unknown
