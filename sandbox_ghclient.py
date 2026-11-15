"""sandbox_ghclient.py - typed GitHub operations through PyGithub.

Why a client AND the `gh` wrapper: `sandbox_github.gh_api` returns raw REST
dicts and exact bytes for file contents, and the launcher depends on both.
PyGithub's low-level requester is not a drop-in for that (checked 2026-11-15
against a real local server: it adds a `url` key to every dict, and mangles raw
file content: text is wrapped in {"data": ...}, a raw JSON file is parsed into
a dict, binary is corrupted). Its value is the typed object model, so this
module wraps exactly the operations a caller needs and returns small frozen
dataclasses; callers move over one at a time and `gh_api` stays for the rest.

Auth is still the user's `gh` login (`gh auth token`), so there is no new login
step and nothing is stored. Settings PyGithub gets wrong for us are overridden:
it sleeps 1 s between writes and 0.25 s between requests by default, and its
default retry waits out rate limits; here writes are never retried (a retried
POST could duplicate a comment) and only idempotent GETs are, a couple of
times. Library exceptions are mapped to GitHubError so callers keep one type.
"""
import contextlib
import dataclasses
import os

import sandbox_github
from sandbox_github import GitHubError

TIMEOUT = 30


@dataclasses.dataclass(frozen=True)
class PrInfo:
    number: int
    base_sha: str
    head_sha: str
    head_ref: str
    head_repo: str      # "owner/name" of the head's repository ("" if it was deleted)
    url: str


def api_base(environ=None):
    """REST base URL: github.com, or GH_HOST's API (as the gh CLI does)."""
    host = (environ if environ is not None else os.environ).get("GH_HOST", "")
    return ("https://api.github.com" if host in ("", "github.com")
            else f"https://{host}/api/v3")


def ca_bundle(environ=None):
    env = environ if environ is not None else os.environ
    return env.get("REQUESTS_CA_BUNDLE") or env.get("SSL_CERT_FILE") or True


def gh_token():
    """The token of the user's `gh` login. Never logged or stored."""
    r = sandbox_github._gh(["auth", "token"])
    token = (r.stdout or "").strip()
    if r.failed or not token:
        raise GitHubError("gh is not logged in: run `gh auth login` (or set "
                          "GH_TOKEN) and retry", 0)
    return token


def make_github(token, base_url=None, verify=None):
    """A PyGithub client configured for this launcher (see module docstring)."""
    from github import Auth, Github
    from urllib3.util.retry import Retry
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(502, 503, 504),
                  allowed_methods=frozenset({"GET", "HEAD"}),
                  raise_on_status=False)
    return Github(auth=Auth.Token(token), base_url=base_url or api_base(),
                  verify=ca_bundle() if verify is None else verify,
                  timeout=TIMEOUT, retry=retry, seconds_between_requests=None,
                  seconds_between_writes=None, user_agent="claude-sandbox",
                  lazy=True)   # get_repo() etc. make no request until used


@contextlib.contextmanager
def _mapped():
    """PyGithub exceptions -> GitHubError(message, http status)."""
    from github import GithubException
    try:
        yield
    except GithubException as e:
        data = e.data if isinstance(e.data, dict) else {}
        raise GitHubError(f"{data.get('message') or e}"[:300] + f" (HTTP {e.status})",
                          e.status) from None


class GhClient:
    """The few GitHub operations the launcher makes through PyGithub."""

    def __init__(self, github):
        self.github = github

    @classmethod
    def from_environment(cls):
        return cls(make_github(gh_token()))

    def _repo(self, repo):
        return self.github.get_repo(repo)

    def login(self):
        with _mapped():
            return self.github.get_user().login

    def pr(self, repo, number):
        with _mapped():
            pr = self._repo(repo).get_pull(int(number))
            head_repo = pr.head.repo.full_name if pr.head.repo else ""
            return PrInfo(pr.number, pr.base.sha, pr.head.sha, pr.head.ref,
                          head_repo, pr.html_url)

    def post_status(self, repo, sha, state, context, description, target_url=None):
        with _mapped():
            commit = self._repo(repo).get_commit(sha)
            kw = {"target_url": target_url} if target_url else {}
            commit.create_status(state=state, description=description,
                                 context=context, **kw)

    def comment(self, repo, number, body):
        with _mapped():
            self._repo(repo).get_issue(int(number)).create_comment(body)

    def ensure_label(self, repo, name, color, description):
        """Create a label; an existing one (422) is fine, a refusal is not
        fatal to callers (they treat labels as best effort)."""
        try:
            with _mapped():
                self._repo(repo).create_label(name, color, description)
        except GitHubError as e:
            if e.code != 422:
                raise

    def add_label(self, repo, number, name):
        with _mapped():
            self._repo(repo).get_issue(int(number)).add_to_labels(name)

    def remove_label(self, repo, number, name):
        """Remove a label if present (absent is fine)."""
        try:
            with _mapped():
                self._repo(repo).get_issue(int(number)).remove_from_labels(name)
        except GitHubError as e:
            if e.code != 404:
                raise
