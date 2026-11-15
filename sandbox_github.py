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


def gh_api(method, path, token, body=None, raw=False):
    """Call the GitHub API; raw=True returns the response body as bytes."""
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
            data = r.read()
            if raw:
                return data
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        raise GitHubError(f"GitHub {method} {path} -> {e.code}: "
                          f"{e.read().decode(errors='replace')}", e.code) from None
