"""sandbox_github.py - minimal GitHub REST client and token lookup (host side)."""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

from sandbox_util import run

API = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")


def validate_repo(repo):
    """Return repo if it looks like owner/name, else raise ValueError."""
    if not REPO_RE.match(repo) or repo.endswith(".git") or ".." in repo:
        raise ValueError(f"not a valid owner/name repo: {repo!r}")
    return repo


def github_token():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return run(["gh", "auth", "token"], capture=True).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("No GitHub token: set GITHUB_TOKEN or run `gh auth login`.")


class GitHubError(RuntimeError):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def _request(method, path, token, body=None, raw=False):
    """Call the GitHub API; returns (body bytes, response headers)."""
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": ("application/vnd.github.raw+json" if raw
                       else "application/vnd.github+json"),
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), r.headers
    except urllib.error.HTTPError as e:
        raise GitHubError(f"GitHub {method} {path} -> {e.code}: "
                          f"{e.read().decode(errors='replace')}", e.code) from None


def gh_api(method, path, token, body=None, raw=False):
    """Call the GitHub API; raw=True returns the response body as bytes."""
    data, _ = _request(method, path, token, body, raw)
    if raw:
        return data
    return json.loads(data) if data else None


def token_scopes(token):
    """OAuth scopes of a classic/OAuth token, or None if GitHub doesn't say
    (fine-grained tokens and GitHub App tokens send no X-OAuth-Scopes)."""
    _, headers = _request("GET", "/", token)
    scopes = headers.get("X-OAuth-Scopes")
    if scopes is None:
        return None
    return {s.strip() for s in scopes.split(",") if s.strip()}


WORKFLOW_FIX = ("to allow it, run `gh auth refresh -s workflow` on the host "
                "(or use a GITHUB_TOKEN with the `workflow` scope / fine-grained "
                "\"Workflows: read & write\") and relaunch")


def workflow_push_warning(scopes):
    """Warning text if keys made with a token of these scopes can't push
    .github/workflows/ changes, else None.

    Deploy and account keys created through an OAuth token are limited to
    that token's scopes when pushing.
    """
    if scopes is None:
        return ("couldn't verify the GitHub token's `workflow` scope "
                "(fine-grained token?); pushes to .github/workflows/ may be "
                "rejected. If so, " + WORKFLOW_FIX)
    if "workflow" not in scopes:
        return ("the GitHub token lacks the `workflow` scope, so the sandbox "
                "can't push changes to .github/workflows/; " + WORKFLOW_FIX)
    return None
