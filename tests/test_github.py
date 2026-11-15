"""Tests for sandbox_github: repo validation, token lookup, API client."""
import io
import json
import subprocess
import urllib.error

import pytest

import sandbox_github as g


@pytest.mark.parametrize("repo", ["me/proj", "my-org/my.repo_1", "a/b"])
def test_validate_repo_accepts(repo):
    assert g.validate_repo(repo) == repo


@pytest.mark.parametrize("repo", ["proj", "a/b/c", "a/b;rm -rf", "-x/y", "a/..",
                                  "a/b.git", "a/ b", ""])
def test_validate_repo_rejects(repo):
    with pytest.raises(ValueError):
        g.validate_repo(repo)


def test_github_token_prefers_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t1")
    monkeypatch.setenv("GH_TOKEN", "t2")
    assert g.github_token() == "t1"


def test_github_token_falls_back_to_gh(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(g, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="tok\n"))
    assert g.github_token() == "tok"


def test_github_token_missing_exits(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def no_gh(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(g, "run", no_gh)
    with pytest.raises(SystemExit):
        g.github_token()


class _Resp(io.BytesIO):
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_gh_api_sends_auth_and_body(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["req"] = req
        return _Resp(b'{"id": 7}')
    monkeypatch.setattr(g.urllib.request, "urlopen", fake_urlopen)
    assert g.gh_api("POST", "/user/keys", "TOKEN", {"key": "k"}) == {"id": 7}
    req = seen["req"]
    assert req.full_url == g.API + "/user/keys"
    assert req.get_method() == "POST"
    assert req.headers["Authorization"] == "Bearer TOKEN"
    assert json.loads(req.data) == {"key": "k"}


def test_gh_api_raw_and_empty(monkeypatch):
    monkeypatch.setattr(g.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b"raw bytes"))
    assert g.gh_api("GET", "/x", "t", raw=True) == b"raw bytes"
    monkeypatch.setattr(g.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(b""))
    assert g.gh_api("DELETE", "/x", "t") is None


def test_gh_api_http_error_carries_code(monkeypatch):
    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {},
                                     io.BytesIO(b"Not Found"))
    monkeypatch.setattr(g.urllib.request, "urlopen", fail)
    with pytest.raises(g.GitHubError) as e:
        g.gh_api("GET", "/missing", "t")
    assert e.value.code == 404 and "Not Found" in str(e.value)


class _Headers(_Resp):
    def __init__(self, headers):
        super().__init__(b"{}")
        self.headers = headers


@pytest.mark.parametrize("header, expect", [
    ("repo, workflow, admin:public_key", {"repo", "workflow", "admin:public_key"}),
    ("", set()),
    (None, None),
])
def test_token_scopes(monkeypatch, header, expect):
    headers = {} if header is None else {"X-OAuth-Scopes": header}
    monkeypatch.setattr(g.urllib.request, "urlopen",
                        lambda req, timeout: _Headers(headers))
    assert g.token_scopes("t") == expect


def test_workflow_push_warning():
    assert g.workflow_push_warning({"repo", "workflow"}) is None
    missing = g.workflow_push_warning({"repo"})
    assert "lacks the `workflow` scope" in missing
    assert "gh auth refresh -s workflow" in missing
    unknown = g.workflow_push_warning(None)
    assert "couldn't verify" in unknown and "gh auth refresh" in unknown
