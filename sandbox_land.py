"""sandbox_land.py - turn a session's pushed branches into pull requests.

Runs on the host with the user's own `gh` login, after the session ends. The
sandbox never holds a credential that can open or merge a PR: it can only push
branches (deploy key), and everything it wrote is treated as untrusted text.

Deliberately NOT done here: merging. Merging stays a human click (or GitHub's
own auto-merge / merge queue, configured by the repo owner), because a PR that
merges itself would let one prompt-injected session change `main`.

A branch is *held* (no PR opened, the user is told) when it touches files that
execute with extra privilege: CI workflows (a same-repo PR runs the branch's
workflow code with the repo's Actions secrets), the deps/host files and
`setup:` scripts (run as root / with the deploy key in every future sandbox),
git hooks and CODEOWNERS.
"""
import urllib.parse

import sandbox_deps
import sandbox_host
from sandbox_github import gh_api
from sandbox_sessions import BRANCH_ROOT, branch_prefix

SENSITIVE_DIRS = (".github/", ".githooks/")
SENSITIVE_FILES = (sandbox_deps.DEPS_FILE, sandbox_host.HOST_FILE)
MAX_SUBJECTS = 20


def sensitive_paths(files, extra=()):
    """The subset of `files` that must be reviewed before any PR exists."""
    exact = {p.lower() for p in (*SENSITIVE_FILES, *extra)}
    hits = []
    for f in files:
        low = f.lower()
        if (low.startswith(SENSITIVE_DIRS) or low in exact
                or low.rsplit("/", 1)[-1] == "codeowners"):
            hits.append(f)
    return sorted(hits)


def clean_text(text, limit):
    """Make untrusted text safe to paste into a PR: one line, no control
    characters, no backticks (the body fences it), and no live @-mentions."""
    t = "".join(c if c.isprintable() else " " for c in text)
    t = " ".join(t.split()).replace("`", "'").replace("@", "@​")
    return t if len(t) <= limit else t[:limit - 1] + "…"


def pr_text(session, branch, subjects):
    """(title, body) for a session branch from its commit subjects."""
    tail = branch.split("/", 2)[-1]
    first = clean_text(subjects[0], 80) if subjects else tail
    title = f"[{session}] " + (first if len(subjects) == 1 else
                               f"{clean_text(tail, 40)} ({len(subjects)} commits)")
    shown = "\n".join(clean_text(s, 120) for s in subjects[:MAX_SUBJECTS])
    more = len(subjects) - MAX_SUBJECTS
    body = (f"Opened by claude_sandbox when session `{session}` ended, from "
            f"branch `{clean_text(branch, 120)}`.\n\n"
            "Everything below was written by an AI session and is untrusted: "
            "review the diff, not this text.\n\n"
            f"````\n{shown}\n````\n"
            + (f"\n...and {more} more commits.\n" if more > 0 else ""))
    return title, body


def _q(s):
    return urllib.parse.quote(s, safe="/")


def session_branches(repo, session=None):
    """Branch names under sbx/<session>/ (or every session's, if None)."""
    prefix = branch_prefix(session) if session else BRANCH_ROOT + "/"
    refs = gh_api("GET", f"/repos/{repo}/git/matching-refs/heads/"
                         f"{_q(prefix)}?per_page=100") or []
    names = [r["ref"][len("refs/heads/"):] for r in refs]
    return [n for n in names if n.startswith(prefix)]


def land_branch(repo, branch, base, extra_sensitive=()):
    """Open a PR for one branch. Returns (outcome, detail):
    'empty' (nothing ahead of base), 'exists' (open PR url), 'held'
    (sensitive files), or 'opened' (PR url)."""
    session = branch.split("/")[1] if branch.count("/") >= 2 else "unknown"
    cmp_ = gh_api("GET", f"/repos/{repo}/compare/{_q(base)}...{_q(branch)}")
    if not cmp_.get("ahead_by"):
        return "empty", ""
    files = [f["filename"] for f in cmp_.get("files") or []]
    if len(files) >= 300:  # the API truncates; assume the worst
        return "held", "300+ files changed (list truncated)"
    hits = sensitive_paths(files, extra_sensitive)
    if hits:
        return "held", ", ".join(hits[:5]) + (" ..." if len(hits) > 5 else "")
    owner = repo.split("/")[0]
    open_ = gh_api("GET", f"/repos/{repo}/pulls?state=open&per_page=1&head="
                          f"{urllib.parse.quote(owner + ':' + branch, safe='')}")
    if open_:
        return "exists", open_[0]["html_url"]
    subjects = [c["commit"]["message"].split("\n", 1)[0]
                for c in cmp_.get("commits") or []]
    title, body = pr_text(session, branch, subjects)
    pr = gh_api("POST", f"/repos/{repo}/pulls",
                {"title": title, "head": branch, "base": base, "body": body,
                 "maintainer_can_modify": False})
    return "opened", pr["html_url"]


def land(repo, session=None, extra_sensitive=()):
    """Open PRs for a session's branches (all sessions' if session is None).
    Returns [(branch, outcome, detail)]."""
    base = gh_api("GET", f"/repos/{repo}")["default_branch"]
    return [(b, *land_branch(repo, b, base, extra_sensitive))
            for b in session_branches(repo, session)]
