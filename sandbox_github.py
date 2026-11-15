"""sandbox_github.py - GitHub access via the host's `gh` CLI (run with invoke)."""
import io
import json
import re
import shlex

from invoke import Context
from invoke.exceptions import CommandTimedOut

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
TIMEOUT = 60
# Never let gh prompt or phone home while we drive it non-interactively.
GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
          "GH_SPINNER_DISABLED": "1", "NO_COLOR": "1"}
_HTTP_RE = re.compile(r"\(HTTP (\d{3})\)")


def validate_repo(repo):
    """Return repo if it looks like owner/name, else raise ValueError."""
    if not REPO_RE.match(repo) or repo.endswith(".git") or ".." in repo:
        raise ValueError(f"not a valid owner/name repo: {repo!r}")
    return repo


def resolve_repo(name, user):
    """'name' -> 'user/name'; 'owner/name' is kept. Validated either way."""
    if "/" not in name:
        if not NAME_RE.match(name) or name in (".", ".."):
            raise ValueError(f"not a valid repository name: {name!r}")
        name = f"{user}/{name}"
    return validate_repo(name)


class GitHubError(RuntimeError):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


# GitHub is reached only through the `gh` CLI, driven locally with invoke. gh
# owns authentication (its own login, or GH_TOKEN/GITHUB_TOKEN), the API
# version and TLS, and is kept current by the host's package manager, so this
# module holds no token and no HTTP client to maintain.
def _gh(args, body=None):
    """Run `gh <args>` locally (no shell parsing of our strings) -> Result."""
    try:
        return Context().run(
            shlex.join(["gh", *args]), hide=True, warn=True, env=GH_ENV,
            timeout=TIMEOUT,
            in_stream=io.StringIO(body) if body is not None else False)
    except CommandTimedOut:
        raise GitHubError(f"gh {args[0]} timed out after {TIMEOUT}s", 0) from None


def _fail(args, r):
    if r.exited == 127:
        raise GitHubError("the GitHub CLI `gh` is not installed on the host; "
                          "install it and run `gh auth login`", 0)
    text = (r.stderr.strip() or r.stdout.strip())[:500]
    m = _HTTP_RE.search(r.stderr)
    raise GitHubError(f"gh {' '.join(args[:3])} failed: {text}",
                      int(m.group(1)) if m else 0)


def gh_api(method, path, body=None, raw=False):
    """Call the GitHub API through `gh api`; raw=True returns bytes."""
    args = ["api", "-X", method, path]
    if raw:
        args += ["-H", "Accept: application/vnd.github.raw+json"]
    if body is not None:
        args += ["--input", "-"]
    r = _gh(args, json.dumps(body) if body is not None else None)
    if r.failed:
        _fail(args, r)
    if raw:
        return r.stdout.encode()
    return json.loads(r.stdout) if r.stdout.strip() else None


def github_login():
    """Login of the account `gh` is authenticated as (also proves auth works)."""
    args = ["api", "user", "--jq", ".login"]
    r = _gh(args)
    if r.failed:
        _fail(args, r)
    return r.stdout.strip()


def token_scopes():
    """OAuth scopes of gh's token, or None if GitHub doesn't say
    (fine-grained tokens and GitHub App tokens send no X-OAuth-Scopes)."""
    args = ["api", "-i", "/"]
    r = _gh(args)
    if r.failed:
        _fail(args, r)
    for line in r.stdout.split("\n\n", 1)[0].splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "x-oauth-scopes":
            return {s.strip() for s in value.split(",") if s.strip()}
    return None


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
