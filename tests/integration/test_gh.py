"""Contract tests for gh against the REAL `gh` binary.

Hermetic (needs no network or login): gh is pointed at a throwaway local HTTPS
server (GH_HOST + a self-signed cert via SSL_CERT_FILE), so these exercise the
exact argv, stdin body, headers and error text between gh and gh
that the mocked unit tests cannot. Skipped unless `gh` and `openssl` are on
PATH. gh's version floats with the host, so this is what catches an upstream
change in flags or output.

A second, opt-in group (CS_GH_LIVE=1, ./scripts/smoke.sh) makes read-only calls to real
GitHub with whatever credentials `gh` has.
"""
import http.server
import json
import os
import shutil
import ssl
import subprocess
import threading

import pytest

from claude_sandbox.github import gh as g
pytestmark = pytest.mark.gh

if not (shutil.which("gh") and shutil.which("openssl")):
    pytest.skip("needs the gh and openssl binaries", allow_module_level=True)


class FakeGitHub:
    """Records requests; replies by path suffix."""

    def __init__(self, cert, key):
        self.requests = []
        self.scopes = "repo, workflow"
        self.cert = str(cert)
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _body(self):
                if self.headers.get("Transfer-Encoding") == "chunked":
                    out = b""
                    while True:
                        n = int(self.rfile.readline().strip() or b"0", 16)
                        if n == 0:
                            self.rfile.readline()
                            return out
                        out += self.rfile.read(n)
                        self.rfile.readline()
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def reply(self, code, body=b"", extra=()):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in extra:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self):
                fake.requests.append({
                    "method": self.command, "path": self.path,
                    "accept": self.headers.get("Accept"),
                    "auth": self.headers.get("Authorization"),
                    "body": self._body().decode()})
                if self.path.endswith("/missing"):
                    return self.reply(404, b'{"message":"Not Found"}')
                if self.path.endswith("/empty"):
                    return self.reply(204)
                if self.path.endswith("/conflict"):
                    return self.reply(409, b'{"message":"Conflict: set by org"}')
                if self.path.endswith("/forbidden"):
                    return self.reply(403, b'{"message":"Must have admin rights"}')
                extra = ([("X-OAuth-Scopes", fake.scopes)]
                         if fake.scopes is not None else [])
                self.reply(200, b'{"login":"octo","id":7}', extra)
            do_GET = do_POST = do_PUT = do_DELETE = handle_any

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket,
                                             server_side=True)
        threading.Thread(target=self.server.serve_forever, args=(0.01,),
                         daemon=True).start()

    @property
    def host(self):
        return f"localhost:{self.server.server_address[1]}"


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    """One throwaway self-signed cert for localhost, shared by the module."""
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "c.pem", d / "k.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
         "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "2", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True)
    return cert, key


@pytest.fixture
def api(tls, tmp_path, monkeypatch):
    fake = FakeGitHub(*tls)
    for var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "ghconfig"))
    monkeypatch.setenv("GH_HOST", fake.host)
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "test-token")
    monkeypatch.setenv("SSL_CERT_FILE", fake.cert)
    yield fake
    fake.server.shutdown()


def test_github_login(api):
    assert g.github_login() == "octo"
    req = api.requests[0]
    assert req["method"] == "GET" and req["path"] == "/api/v3/user"
    assert req["auth"].endswith("test-token")  # gh supplies auth, not us


def test_post_body_arrives_intact_on_stdin(api):
    body = {"title": "t", "key": "ssh-ed25519 AAAA'\"; rm -rf ~ $(x) `y`\n",
            "read_only": False}
    assert g.gh_api("POST", "/repos/me/p/keys", body) == {"login": "octo", "id": 7}
    req = api.requests[0]
    assert req["method"] == "POST" and req["path"] == "/api/v3/repos/me/p/keys"
    assert json.loads(req["body"]) == body


def test_raw_sends_the_raw_accept_header_and_returns_bytes(api):
    out = g.gh_api("GET", "/repos/me/p/contents/f.yaml", raw=True)
    assert isinstance(out, bytes) and b"octo" in out
    assert api.requests[0]["accept"] == "application/vnd.github.raw+json"


def test_404_maps_to_githuberror_with_code(api):
    with pytest.raises(g.GitHubError) as e:
        g.gh_api("GET", "/repos/me/p/contents/missing")
    assert e.value.code == 404 and "Not Found" in str(e.value)


def test_put_body_arrives_intact_for_the_actions_settings(api):
    """The Actions-hardening fixes are PUTs of one field each."""
    path = "/repos/me/p/actions/permissions/workflow"
    g.gh_api("PUT", path, {"default_workflow_permissions": "read"})
    g.gh_api("PUT", path, {"can_approve_pull_request_reviews": False})
    first, second = api.requests
    assert first["method"] == second["method"] == "PUT"
    assert first["path"] == "/api/v3" + path
    assert json.loads(first["body"]) == {"default_workflow_permissions": "read"}
    assert json.loads(second["body"]) == {"can_approve_pull_request_reviews": False}


@pytest.mark.parametrize("tail,code", [("conflict", 409), ("forbidden", 403)])
def test_an_org_policy_or_missing_admin_surfaces_as_a_coded_error(api, tail, code):
    """409 (an organisation forbids the change) and 403 (no admin) are the two
    refusals hardening reports instead of failing on."""
    from claude_sandbox.github import hardening as sh
    with pytest.raises(g.GitHubError) as e:
        g.gh_api("PUT", f"/repos/me/p/actions/permissions/workflow/{tail}",
                 {"default_workflow_permissions": "read"})
    assert e.value.code == code
    ok, msg = sh.apply_fix(
        ("PUT", f"/repos/me/p/actions/permissions/workflow/{tail}",
         {"default_workflow_permissions": "read"}), g.gh_api)
    assert not ok and msg.startswith("not applied")


def test_empty_response_is_none(api):
    assert g.gh_api("DELETE", "/repos/me/p/keys/empty") is None
    assert api.requests[0]["method"] == "DELETE"


def test_token_scopes_parsed_from_real_header_output(api):
    assert g.token_scopes() == {"repo", "workflow"}
    api.scopes = None  # fine-grained / app tokens send no header
    assert g.token_scopes() is None


def test_hostile_path_is_one_argument_never_a_shell_command(api, tmp_path):
    pwn = tmp_path / "pwned"
    g.gh_api("GET", f"/repos/me/p/contents/a; touch {pwn}; $(touch {pwn})")
    assert not pwn.exists()
    assert len(api.requests) == 1


def test_not_logged_in_error_tells_the_user_what_to_do(api, monkeypatch):
    """For github.com gh refuses before any network call (exit 4)."""
    monkeypatch.delenv("GH_HOST")
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN")
    with pytest.raises(g.GitHubError, match="gh auth login") as e:
        g.github_login()
    assert e.value.code == 0 and api.requests == []


def test_gh_missing_is_a_clear_error(api, monkeypatch, tmp_path):
    only_bash = tmp_path / "only-bash"
    only_bash.mkdir()
    (only_bash / "bash").symlink_to(shutil.which("bash"))
    monkeypatch.setenv("PATH", str(only_bash))  # a shell, but no gh
    with pytest.raises(g.GitHubError, match="not installed"):
        g.github_login()


# --------------------------------------------------------------------------- #
# opt-in: read-only calls to real GitHub (./scripts/smoke.sh, or CI with GH_TOKEN)
# --------------------------------------------------------------------------- #
live = pytest.mark.skipif(os.environ.get("CS_GH_LIVE") != "1",
                          reason="set CS_GH_LIVE=1 to call real GitHub")


@live
def test_live_meta_has_ssh_host_keys():
    assert g.gh_api("GET", "/meta")["ssh_keys"]  # what provision_key pins


@live
def test_live_raw_contents_of_a_public_file():
    out = g.gh_api("GET", "/repos/cli/cli/contents/README.md", raw=True)
    assert b"GitHub CLI" in out


@live
def test_live_missing_file_is_404():
    with pytest.raises(g.GitHubError) as e:
        g.gh_api("GET", "/repos/cli/cli/contents/no-such-file-claude-sandbox")
    assert e.value.code == 404


@live
def test_live_token_scopes_is_a_set_or_none():
    scopes = g.token_scopes()
    assert scopes is None or isinstance(scopes, set)


@live
def test_live_login_is_a_name_or_a_coded_auth_error():
    """A user token gives a login; CI's installation token is refused (403)."""
    try:
        assert g.github_login()
    except g.GitHubError as e:
        assert e.code in (401, 403)
