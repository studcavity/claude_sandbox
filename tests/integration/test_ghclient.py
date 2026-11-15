"""sandbox_ghclient against REAL PyGithub, over TLS, with no GitHub involved.

A throwaway local HTTPS server stands in for the API (the same trick as
test_gh.py), so these check what the unit tests can't: the actual requests
PyGithub sends (paths, JSON bodies, auth header), that its defaults that would
hurt us are really off (1 s sleeps between writes, retrying a POST), and how
real library exceptions map onto GitHubError. Needs openssl for the cert.
"""
import http.server
import json
import shutil
import ssl
import subprocess
import threading
import time

import pytest

from claude_sandbox.github import client as gc
from claude_sandbox.github.gh import GitHubError

pytestmark = pytest.mark.gh

if not shutil.which("openssl"):
    pytest.skip("needs the openssl binary", allow_module_level=True)

SHA = "a" * 40


class Server:
    def __init__(self, cert, key):
        self.requests, self.overrides = [], {}   # overrides: (method, path) -> [codes]
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                p = self.path.split("?")[0].removeprefix("/api/v3")
                srv.requests.append({"method": self.command, "path": p, "body": body,
                                     "auth": self.headers.get("Authorization"),
                                     "t": time.monotonic()})
                queued = srv.overrides.get((self.command, p))
                if queued:
                    code = queued.pop(0)
                    if code != 200:
                        return self.send(code, {"message": f"forced {code}"})
                self.route(self.command, p, body)

            def send(self, code, data=None):
                raw = json.dumps(data if data is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def route(self, m, p, body):
                base = f"https://localhost:{srv.port}/api/v3"
                if p == "/user":
                    return self.send(200, {"login": "octo"})
                if m == "GET" and p == "/repos/o/r/pulls/7":
                    return self.send(200, {
                        "number": 7, "html_url": "https://github.com/o/r/pull/7",
                        "base": {"sha": "b" * 40, "ref": "main"},
                        "head": {"sha": SHA, "ref": "sbx/s/work",
                                 "repo": {"full_name": "o/r"}}})
                if m == "GET" and p == "/repos/o/r/pulls/8":      # head repo deleted
                    return self.send(200, {"number": 8, "html_url": "u",
                                           "base": {"sha": "b" * 40, "ref": "main"},
                                           "head": {"sha": SHA, "ref": "x", "repo": None}})
                if m == "GET" and p.startswith("/repos/o/r/commits/"):
                    sha = p.rsplit("/", 1)[1]
                    return self.send(200, {"sha": sha, "url": f"{base}{p}"})
                if m == "POST" and p.startswith("/repos/o/r/statuses/"):
                    return self.send(201, {"state": json.loads(body)["state"]})
                if m == "GET" and p.startswith("/repos/o/r/issues/"):
                    return self.send(200, {"number": 7, "url": f"{base}{p}"})
                if m == "POST" and p.endswith("/comments"):
                    return self.send(201, {"id": 1, "body": json.loads(body)["body"]})
                if m == "POST" and p.endswith("/labels") and "/issues/" in p:
                    # PyGithub sends a bare JSON list here, not {"labels": [...]}
                    return self.send(200, [{"name": n} for n in json.loads(body)])
                if m == "DELETE" and "/labels/" in p:
                    return self.send(200, [])
                if m == "POST" and p == "/repos/o/r/labels":
                    return self.send(201, {"name": json.loads(body)["name"]})
                self.send(404, {"message": "Not Found"})
            do_GET = do_POST = do_DELETE = do_PUT = do_PATCH = _go

            def log_message(self, *a):
                pass

        self.http = http.server.HTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.http.socket = ctx.wrap_socket(self.http.socket, server_side=True)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, args=(0.01,), daemon=True).start()

    def calls(self, method=None):
        return [r for r in self.requests if method is None or r["method"] == method]


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "c.pem", d / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "2", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    return cert, key


@pytest.fixture
def srv(tls):
    s = Server(*tls)
    yield s
    s.http.shutdown()


@pytest.fixture
def client(srv, tls):
    gh = gc.make_github("tok123", base_url=f"https://localhost:{srv.port}/api/v3",
                        verify=str(tls[0]))
    return gc.GhClient(gh)


def test_login_and_the_auth_header(client, srv):
    assert client.login() == "octo"
    assert srv.requests[0]["auth"].split()[-1] == "tok123"


def test_pr_info_is_typed_and_maps_a_deleted_head_repo_to_empty(client):
    pr = client.pr("o/r", 7)
    assert pr == gc.PrInfo(7, "b" * 40, SHA, "sbx/s/work", "o/r", "https://github.com/o/r/pull/7")
    assert client.pr("o/r", 8).head_repo == ""
    with pytest.raises(Exception):
        pr.number = 9                                     # frozen


def test_post_status_sends_exactly_the_fields_the_gate_needs(client, srv):
    client.post_status("o/r", SHA, "success", "sandbox/provenance", "0 errors, 0 warnings",
                       "https://github.com/o/r/pull/7")
    (post,) = srv.calls("POST")
    assert post["path"] == f"/repos/o/r/statuses/{SHA}"
    assert json.loads(post["body"]) == {
        "state": "success", "target_url": "https://github.com/o/r/pull/7",
        "description": "0 errors, 0 warnings", "context": "sandbox/provenance"}
    client.post_status("o/r", SHA, "error", "sandbox/provenance", "check crashed")
    assert "target_url" not in json.loads(srv.calls("POST")[-1]["body"])


def test_comments_hit_the_issue_endpoint(client, srv):
    client.comment("o/r", 7, "overridden by octo")
    seen = [(r["method"], r["path"]) for r in srv.requests if r["method"] != "GET"]
    assert ("POST", "/repos/o/r/issues/7/comments") in seen


def test_library_defaults_that_would_slow_every_write_are_off(client, srv):
    """PyGithub sleeps 1 s between writes and 0.25 s between requests by default."""
    start = time.monotonic()
    for i in range(5):
        client.post_status("o/r", SHA, "success", "sandbox/provenance", f"d{i}")
    assert time.monotonic() - start < 3.0        # the defaults alone would be >= 8 s


def test_a_failed_write_is_never_retried_but_a_failed_read_is(client, srv):
    srv.overrides[("POST", f"/repos/o/r/statuses/{SHA}")] = [503, 200]
    with pytest.raises(GitHubError) as e:
        client.post_status("o/r", SHA, "success", "sandbox/provenance", "x")
    assert e.value.code == 503
    assert len(srv.calls("POST")) == 1           # a retried POST could duplicate

    srv.overrides[("GET", "/user")] = [503, 200]
    assert client.login() == "octo"              # idempotent: retried and succeeded
    assert len([r for r in srv.calls("GET") if r["path"] == "/user"]) == 2


@pytest.mark.parametrize("code", [404, 403, 401, 500])
def test_http_errors_become_githuberror_with_the_status(client, srv, code):
    srv.overrides[("GET", "/repos/o/r/pulls/7")] = [code]
    with pytest.raises(GitHubError) as e:
        client.pr("o/r", 7)
    assert e.value.code == code and f"HTTP {code}" in str(e.value)


def test_gh_token_comes_from_the_gh_login_and_is_never_in_an_error(monkeypatch):
    import types
    from claude_sandbox.github import gh as sandbox_github
    monkeypatch.setattr(sandbox_github, "_gh", lambda args, body=None: types.SimpleNamespace(
        failed=False, stdout="ghp_secretvalue\n", stderr=""))
    assert gc.gh_token() == "ghp_secretvalue"
    monkeypatch.setattr(sandbox_github, "_gh", lambda args, body=None: types.SimpleNamespace(
        failed=True, stdout="", stderr="gh: not logged in ghp_secretvalue"))
    with pytest.raises(GitHubError) as e:
        gc.gh_token()
    assert "ghp_" not in str(e.value) and "gh auth login" in str(e.value)


def test_base_url_and_ca_bundle_follow_the_environment():
    assert gc.api_base({}) == "https://api.github.com"
    assert gc.api_base({"GH_HOST": "github.com"}) == "https://api.github.com"
    assert gc.api_base({"GH_HOST": "ghe.example.com"}) == "https://ghe.example.com/api/v3"
    assert gc.ca_bundle({}) is True
    assert gc.ca_bundle({"SSL_CERT_FILE": "/a"}) == "/a"
    assert gc.ca_bundle({"SSL_CERT_FILE": "/a", "REQUESTS_CA_BUNDLE": "/b"}) == "/b"
