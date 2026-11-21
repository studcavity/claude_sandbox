"""github/gh.py - GitHub access via the host's `gh` CLI (run with invoke)."""
import concurrent.futures
import functools
import io
import json
import os
import re
import shlex
import tempfile
import threading
import time
from typing import Any, Callable

from invoke import Context
from invoke.exceptions import CommandTimedOut

from claude_sandbox import timing
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
    start = time.perf_counter() if timing.ACTIVE else None
    try:
        return Context().run(
            shlex.join(["gh", *args]), hide=True, warn=True, env=GH_ENV,
            timeout=TIMEOUT,
            in_stream=io.StringIO(body) if body is not None else False)
    except CommandTimedOut:
        raise GitHubError(f"gh {args[0]} timed out after {TIMEOUT}s", 0) from None
    finally:
        if start is not None:
            timing.count("gh", time.perf_counter() - start)


def _fail(args, r):
    if r.exited == 127:
        raise GitHubError("the GitHub CLI `gh` is not installed on the host; "
                          "install it and run `gh auth login`", 0)
    text = (r.stderr.strip() or r.stdout.strip())[:500]
    if r.exited == 4:  # gh's documented "authentication required" status
        # Our own wording: gh's text varies (e.g. it differs under GitHub
        # Actions), but the exit status is stable.
        raise GitHubError("gh is not logged in: run `gh auth login` (or set "
                          f"GH_TOKEN) and retry. gh said: {text}", 0)
    m = _HTTP_RE.search(r.stderr)
    raise GitHubError(f"gh {' '.join(args[:3])} failed: {text}",
                      int(m.group(1)) if m else 0)


def _gh_api_live(method, path, body=None, raw=False):
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


# --- prefetch -------------------------------------------------------------- #
# A launch makes ~5 independent GitHub reads back to back (who am I, token
# scopes, the deps file, the host file, repo info, deploy keys), ~0.35 s each.
# `start_prefetch` begins them together in threads; the ordinary functions
# below consume a finished (or running) result once if there is one and
# otherwise call GitHub as before, so no call site changes, and an exception
# in a prefetched call replays where the value is used, exactly as a direct
# call would have raised it. Entries expire (PREFETCH_TTL) so a read can never
# be served stale much later, and are consumed at most once.
PREFETCH_ENABLED = True
PREFETCH_TTL = 120.0
_prefetched: dict = {}
_prefetch_lock = threading.Lock()
_pool = None


def _take(key):
    with _prefetch_lock:
        entry = _prefetched.pop(key, None)
    if entry is None or time.monotonic() - entry[1] > PREFETCH_TTL:
        return None
    return entry[0]


def start_prefetch(specs):
    """Begin the reads in `specs` concurrently. Each spec is ("login",),
    ("scopes",) or ("api", path, raw) for a GET. No-op when disabled."""
    global _pool
    if not PREFETCH_ENABLED or not specs:
        return
    with _prefetch_lock:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=6, thread_name_prefix="prefetch")
        for spec in specs:
            job: Callable[[], Any]
            key: tuple
            if spec[0] == "api":
                key = ("api", spec[1], bool(spec[2]))
                job = functools.partial(_gh_api_live, "GET", spec[1], None,
                                        bool(spec[2]))
            elif spec[0] == "login":
                key, job = ("login",), _github_login_live
            elif spec[0] == "scopes":
                key, job = ("scopes",), _token_scopes_live
            else:
                raise ValueError(f"unknown prefetch spec {spec!r}")
            _prefetched[key] = (_pool.submit(job), time.monotonic())


def clear_prefetch():
    """Forget anything unconsumed and stop the workers (end of a launch)."""
    global _pool
    with _prefetch_lock:
        _prefetched.clear()
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


def gh_api(method, path, body=None, raw=False):
    """Call the GitHub API through `gh api`; raw=True returns bytes. A GET that
    was prefetched is answered from that (once)."""
    if method == "GET" and body is None:
        fut = _take(("api", path, bool(raw)))
        if fut is not None:
            return fut.result()
    return _gh_api_live(method, path, body, raw)


def _github_login_live():
    """Login of the account `gh` is authenticated as (also proves auth works)."""
    args = ["api", "user", "--jq", ".login"]
    r = _gh(args)
    if r.failed:
        _fail(args, r)
    return r.stdout.strip()


def repo_owner(repo):
    """Login of the account that owns `repo`, as GitHub reports it now (so a
    renamed or transferred repo shows its real owner)."""
    return gh_api("GET", f"/repos/{repo}")["owner"]["login"]


def github_login():
    """Login of the account `gh` is authenticated as (also proves auth works)."""
    fut = _take(("login",))
    return fut.result() if fut is not None else _github_login_live()


def _token_scopes_live():
    """OAuth scopes of gh's token, or None if GitHub doesn't say
    (fine-grained tokens and GitHub App tokens send no X-OAuth-Scopes)."""
    args = ["api", "-i", "/"]
    r = _gh(args)
    if r.failed:
        _fail(args, r)
    for line in re.split(r"\r?\n\r?\n", r.stdout, maxsplit=1)[0].splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "x-oauth-scopes":
            return {s.strip() for s in value.split(",") if s.strip()}
    return None


def token_scopes():
    """OAuth scopes of gh's token (see _token_scopes_live), prefetched if started."""
    fut = _take(("scopes",))
    return fut.result() if fut is not None else _token_scopes_live()


WORKFLOW_FIX = ("unless you need sessions to edit CI, drop it: `gh auth "
                "refresh --remove-scopes workflow` on the host (fine-grained "
                "token: remove \"Workflows: write\") and relaunch")


def workflow_push_warning(scopes):
    """Warning text if keys made with a token of these scopes CAN push
    .github/workflows/ changes, else None.

    Deploy and account keys created through an OAuth token are limited to
    that token's scopes when pushing. A pushed workflow with an `on: push`
    trigger runs straight away, before any PR or review, with the repo's
    Actions secrets, so the launcher's hold on PRs that touch .github/ can't
    help. Not having the scope is the safe default and needs no warning.
    """
    if scopes is None:
        return ("couldn't verify the GitHub token's scopes (fine-grained "
                "token?). If it can write workflows, the sandbox can push a "
                "workflow that runs with this repo's Actions secrets before "
                "any review; " + WORKFLOW_FIX)
    if "workflow" in scopes:
        return ("the GitHub token has the `workflow` scope, so the sandbox can "
                "push a workflow that runs with this repo's Actions secrets "
                "before any review; " + WORKFLOW_FIX)
    return None


def verify_attestation(data, repo):
    """Check that the file `data` has a GitHub build attestation from `repo`
    (`gh attestation verify`, run on the host with its login). Raises
    GitHubError when it doesn't, or when gh can't check (too old, offline,
    not logged in): a failed check never means "assume fine".

    The bytes are written to a fresh private directory; the file name is not
    part of the check, only its sha256.
    """
    validate_repo(repo)
    with tempfile.TemporaryDirectory(prefix="cs-attest-") as d:
        path = os.path.join(d, "artifact")
        with open(path, "wb") as f:
            f.write(data)
        r = _gh(["attestation", "verify", path, "--repo", repo])
    if r.exited != 0:
        text = (r.stderr.strip() or r.stdout.strip())[:500]
        raise GitHubError(f"attestation of a download from {repo} did not "
                          f"verify (needs gh >= 2.49, logged in): {text}", 0)
