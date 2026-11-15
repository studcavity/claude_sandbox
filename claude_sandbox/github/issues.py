"""github/issues.py - file the follow-up issues that `todo/` files request.

    python github/issues.py OWNER/REPO [--since REV | --last N] [--dry-run]

A sandboxed session can't touch GitHub beyond pushing, but it can say "someone
outside should do X" by adding `todo/<name>.md` to a commit (the format, and
why the path is the identity, is in `todo`). This module files an
issue for each such file, and it only ever does so for commits that are
ALREADY ON THE DEFAULT BRANCH: a human has reviewed that text before it is
published, and nothing from an unmerged branch is filed. (A request whose PR
is never merged is never filed. That is deliberate: unmerged work is
unreviewed. Deleting the file before merge withdraws it.)

Run it in a checkout of the repo, on the default branch, over the commits that
are new: `--since REV` scans REV..HEAD (the workflow in
`.github/workflows/file-issues.yml` passes the push's `before` sha), `--last N`
the last N commits. It reads the files from the checkout (at the commit that
added them, so a later deletion doesn't matter), never from the network, and
talks to GitHub with the caller's `gh` login or `GITHUB_TOKEN`. In the
workflow that is `GITHUB_TOKEN` with `issues: write`, so the issues are
created by `github-actions[bot]`, not under a person's name.

A request is a commit that ADDS a valid `todo/<name>.md`. Anything else is a
no-op, never an error: a file that fails the format (CI rejects those), one
with no heading, one edited or deleted later. One bad request never affects
the others. The issue's title is the file's first heading; its body is the
file, sanitised and bounded (MAX_BODY), with a permalink. A file that looks
like it holds a credential is refused and reported, never published.

ONE LEDGER. Each issue is created with the labels `sandbox` and `follow-up`
and its body ends with `<!-- claude-sandbox:issue sha=<adding commit>
id=<name> -->`. Before filing, the issue list is searched for that marker on
the LAST line of a body, so running it twice over the same range files
nothing new, and a crash between runs can't duplicate (the issue and its
marker are one write). HTML comments in session text are defanged so a
session can't forge a marker. Only the newest MAX_LEDGER_PAGES x 100
`follow-up` issues are searched; scanning only the new range keeps that
enough. The key is (adding commit, name), so a path added again after a
deletion is a new request.

Trust: everything read here was written by an AI session. It is bounded,
stripped of control characters and live @-mentions, and labelled untrusted.
It only ever opens issues: narrow, reversible, non-merging.
"""
import argparse
import collections
import re
import subprocess
import sys
import urllib.parse

from claude_sandbox.github import labels as github_labels
from claude_sandbox.github import templates
from claude_sandbox.portable import todo
from claude_sandbox.portable import untrusted
MAX_PER_RUN = 25          # requests filed in one run (--max)
MAX_TITLE = 120
MAX_BODY = 6000
MARKER = "claude-sandbox:issue"
MAX_LEDGER_PAGES = 5      # x100 issues scanned for an existing marker
DEFAULT_LAST = 50
_NAME = r"[a-z0-9][a-z0-9-]{0,79}"
_LEDGER_RE = re.compile(r"<!-- " + MARKER + r" sha=([0-9a-f]{7,40}) id=("
                        + _NAME + r") -->\s*$")

# sha: the commit that added the file; path: `todo/<name>.md`; text: the file
# at that commit, or None when it is over the size limit.
Request = collections.namedtuple("Request", "sha path text")
# status: done | exists | rejected | failed
Outcome = collections.namedtuple("Outcome", "sha path status detail")


class IssuesError(ValueError):
    """A refusal, with a message meant for the person running us."""


class Rejected(ValueError):
    """The request doesn't conform: a silent no-op."""


class PolicyRejected(Rejected):
    """The request conforms but we refuse to publish it (e.g. it appears to
    contain a credential). Unlike a malformed one this is reported, so the user
    knows an item was held back and why. The reason names the kind of problem,
    never the offending text."""


class Reused(str):
    """The detail when the issue already existed (found by its marker), so it
    is reported as "exists", not "done"."""


def git(*args, cwd=None):
    # errors="replace": a hostile or merely non-UTF-8 file must not crash us.
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, errors="replace", cwd=cwd).stdout


# --- reading requests from a checkout ---------------------------------------- #
def read_requests(since=None, last=DEFAULT_LAST, head="HEAD", cwd=None):
    """[Request], oldest commit first, for the valid `todo/<name>.md` files
    that `since..head` (or the last N commits up to head) ADD."""
    for r in (since or "", head):
        if r.startswith("-"):
            raise IssuesError(f"not a revision: {r!r}")
    args = ["rev-list", "--reverse"]
    args += [f"{since}..{head}"] if since else [f"--max-count={int(last)}", head]
    out = []
    for sha in git(*args, cwd=cwd).split():
        names = git("diff-tree", "-r", "-z", "--no-commit-id", "--name-only",
                    "--diff-filter=A", "--no-renames", "--root", sha, cwd=cwd)
        for path in sorted(n for n in names.split("\0") if n):
            if not todo.request_name(path):
                continue
            ref = f"{sha}:{path}"
            try:
                size = int(git("cat-file", "-s", ref, cwd=cwd))
                text = (git("show", ref, cwd=cwd)
                        if size <= todo.MAX_BYTES else None)
            except subprocess.CalledProcessError:
                continue    # a symlink or submodule, not a file
            out.append(Request(sha, path, text))
    return out


def clean_block(text, limit):
    """Untrusted multi-line text made safe to paste into GitHub: no control
    characters (newlines kept), no live @-mentions, bounded."""
    return untrusted.block(text, limit)


def clean_line(text, limit):
    return " ".join(clean_block(text, limit * 2).split())[:limit]


def _defang(text):
    """No HTML comments in session text: the marker is how we recognise our own
    issues, so session text must not be able to forge one."""
    return text.replace("<!--", "<!- -")


# --- the ledger --------------------------------------------------------------- #
def known_issues(api, repo):
    """{(sha, name): url} of issues this tool already filed, found by the
    marker on the LAST line of the body."""
    found: dict = {}
    for page in range(1, MAX_LEDGER_PAGES + 1):
        items = api("GET", f"/repos/{repo}/issues?labels="
                    f"{github_labels.FOLLOW_UP}&state=all&per_page=100"
                    f"&page={page}") or []
        for it in items:
            if "pull_request" in it:
                continue
            m = _LEDGER_RE.search(it.get("body") or "")
            if m and (m.group(1), m.group(2)) not in found:
                found[(m.group(1), m.group(2))] = it["html_url"]
        if len(items) < 100:
            break
    return found


def file_issue(api, repo, req, known, labels_done):
    """File one request -> the issue URL (a Reused one if it exists)."""
    name = todo.request_name(req.path)
    if req.text is None:
        raise PolicyRejected(f"not filed: {req.path} is over "
                             f"{todo.MAX_BYTES} bytes")
    title = todo.title(req.text)
    if not title or not clean_line(title, MAX_TITLE):
        raise Rejected("a request needs a heading")
    hits = untrusted.find_secrets(req.text)
    if hits:
        raise PolicyRejected(f"not filed: {req.path} appears to contain "
                             + ", ".join(hits))
    if (req.sha, name) in known:
        return Reused(known[(req.sha, name)])
    labels = [github_labels.SANDBOX, github_labels.FOLLOW_UP]
    github_labels.ensure(api, repo, labels, labels_done)
    body = (f"{_defang(clean_block(req.text, MAX_BODY))}\n\n"
            f"[`{req.path}` at that commit](https://github.com/{repo}"
            f"/blob/{req.sha}/{urllib.parse.quote(req.path)})")
    text = (templates.render("issue.md", commit=req.sha, item=name,
                                     body=body)
            + f"\n<!-- {MARKER} sha={req.sha} id={name} -->")
    issue = api("POST", f"/repos/{repo}/issues",
                {"title": clean_line(title, MAX_TITLE), "body": text,
                 "labels": labels})
    known[(req.sha, name)] = issue["html_url"]
    return issue["html_url"]


def execute(api, repo, requests, limit=MAX_PER_RUN):
    """File requests -> [Outcome]. One failure never stops the rest."""
    out, known = [], None
    labels_done: set = set()
    for req in requests[:limit]:
        try:
            if known is None:
                known = known_issues(api, repo)
            detail = file_issue(api, repo, req, known, labels_done)
            out.append(Outcome(req.sha, req.path,
                               "exists" if isinstance(detail, Reused) else "done",
                               detail))
        except PolicyRejected as e:
            out.append(Outcome(req.sha, req.path, "rejected", str(e)))
        except Rejected:
            continue  # nonconforming request: a no-op
        except Exception as e:
            out.append(Outcome(req.sha, req.path, "failed", str(e)[:200]))
    return out


def format_outcomes(outcomes):
    """Summary lines (plain text); empty if nothing was requested."""
    if not outcomes:
        return ""
    label = {"done": "filed", "exists": "already filed", "failed": "FAILED",
             "rejected": "REJECTED (policy)"}
    lines = ["follow-up issues"]
    for o in outcomes:
        lines.append(f"  {label[o.status]:<18} {o.sha[:7]}:{o.path}"
                     + (f"  {o.detail}" if o.detail else ""))
    return "\n".join(lines)


class DryRunApi:
    """Reads go through; every write is recorded and answered with a stub."""

    def __init__(self, api):
        self.api, self.writes = api, []

    def __call__(self, method, path, body=None, raw=False):
        if method == "GET":
            return self.api(method, path, body, raw=raw)
        self.writes.append((method, path, body))
        return {"html_url": "(dry run: not created)"}


def run(repo, since=None, last=DEFAULT_LAST, dry_run=False, api=None, cwd=None,
        out=print, limit=MAX_PER_RUN):
    """Scan commits for added `todo/` requests and file them -> exit code
    (0 ok, 1 if any item failed)."""
    from claude_sandbox.github import gh as gh
    gh.validate_repo(repo)
    api = api or gh.gh_api
    if dry_run:
        api = DryRunApi(api)
    outcomes = execute(api, repo, read_requests(since, last, cwd=cwd), limit)
    if dry_run:
        for method, path, body in api.writes:
            if path.endswith("/issues") and method == "POST":
                out(f"would file: {body['title']!r} "
                    f"labels={','.join(body['labels'])}")
    text = format_outcomes(outcomes)
    out(text or "no todo/ requests found in "
        + (f"{since}..HEAD" if since else f"the last {last} commits"))
    return 1 if any(o.status == "failed" for o in outcomes) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", metavar="OWNER/REPO")
    ap.add_argument("--since", metavar="REV", help="scan REV..HEAD")
    ap.add_argument("--last", type=int, default=DEFAULT_LAST, metavar="N",
                    help=f"scan the last N commits (default {DEFAULT_LAST}; "
                    "ignored with --since)")
    ap.add_argument("--max", type=int, default=MAX_PER_RUN, metavar="N",
                    help="issues to file in one run (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be filed; write nothing")
    a = ap.parse_args(argv)
    if a.last < 1 or a.max < 1:
        ap.error("--last and --max must be at least 1")
    try:
        return run(a.repo, a.since, a.last, a.dry_run, limit=a.max)
    except IssuesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
