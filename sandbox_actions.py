"""sandbox_actions.py - follow-up issues requested by `todo/` files.

A sandboxed session can't touch GitHub beyond pushing, but it can say "someone
outside should do X" by adding `todo/<name>.md` to a commit (the format, and
why the path is the identity, is in `sandbox_todo`). The launcher reads those
requests on the host and files an issue for each, with the user's own `gh`
login. Only the planning is pure; `execute`/`run` touch GitHub.

A request is a commit that ADDS a valid `todo/<name>.md`. Anything else is a
no-op, never an error: a file that fails the format (CI rejects those), one
that is empty or has no heading, one that is edited or deleted later. One bad
request never affects the others.

The issue's title is the file's first heading; its body is the file, read AT
THE ADDING COMMIT (so a later deletion doesn't matter), sanitised and bounded
(MAX_BODY), with a permalink. The launcher refuses to file one that looks like
it holds a credential.

Issues are created with the labels `sandbox`, `follow-up` and
`session:<session>` (created on first use; best effort). The issue is also the
idempotency ledger: its body ends with `<!-- claude-sandbox:action sha=.. id=..
-->` (id = the file's name), and before filing the launcher looks for an
existing `sandbox`-labelled issue with that marker on its LAST line, so a crash
between creating the issue and commenting on the commit can't duplicate it.
HTML comments in session text are defanged so a session can't forge a marker.
The commit comment remains a second ledger and is healed when the issue
already exists.

Trust: everything here is text written by an AI session. It is bounded,
stripped of control characters and live @-mentions, and labelled untrusted.
It only ever opens issues: narrow, reversible, non-merging.
"""
import collections
import re
import urllib.parse

import sandbox_labels
import sandbox_names
import sandbox_templates
import sandbox_todo
import sandbox_untrusted

MAX_ACTIONS = 25      # executed per run, across all commits (--max-actions)
MAX_ISSUES_PER_SESSION = 10   # issues one session may have, existing + new (--max-issues)
MAX_TITLE = 120
MAX_BODY = 6000
MARKER = "claude-sandbox:action"
MAX_LEDGER_PAGES = 5      # x100 issues scanned for an existing marker
MAX_COMMITS_SCANNED = 100  # commits examined per branch at session end
_NAME = r"[a-z0-9][a-z0-9-]{0,79}"

# sha: the commit that added the file; path: `todo/<name>.md`.
Request = collections.namedtuple("Request", "sha path")
# status: done | exists | rejected | failed
Outcome = collections.namedtuple("Outcome", "sha path status detail")


def name_of(req):
    return sandbox_todo.request_name(req.path)


def requests_from(commit_files):
    """[(sha, [paths the commit ADDED])] -> [Request], in the order given,
    keeping only valid `todo/<name>.md` paths, one per (sha, path)."""
    seen, out = set(), []
    for sha, paths in commit_files:
        for path in paths:
            if sandbox_todo.request_name(path) and (sha, path) not in seen:
                seen.add((sha, path))
                out.append(Request(sha, path))
    return out


def clean_block(text, limit):
    """Untrusted multi-line text made safe to paste into GitHub: no control
    characters (newlines kept), no live @-mentions, bounded."""
    return sandbox_untrusted.block(text, limit)


def clean_line(text, limit):
    return " ".join(clean_block(text, limit * 2).split())[:limit]


# --- the issue handler ------------------------------------------------------ #
class Rejected(ValueError):
    """The request doesn't conform: a silent no-op."""


class PolicyRejected(Rejected):
    """The request conforms but we refuse to publish it (e.g. it appears to
    contain a credential). Unlike a malformed one this is reported in the exit
    summary, so the user knows an item was held back and why. The reason names
    the kind of problem, never the offending text."""


# `cache` is per-run scratch (labels ensured, issues already filed); execute()
# supplies it, so callers can keep building Context(repo, session, branches, api).
Context = collections.namedtuple("Context", "repo session branches api cache limits",
                                 defaults=(None, None))


def limit(ctx, name):
    """A configured cap (`actions` per run, `issues` per session)."""
    default = {"actions": MAX_ACTIONS, "issues": MAX_ISSUES_PER_SESSION}[name]
    return int((ctx.limits or {}).get(name, default))


class Reused(str):
    """A handler's detail when the item already existed (e.g. its issue was
    found by marker), so it is reported as "exists", not "done"."""


_LEDGER_RE = re.compile(r"<!-- " + MARKER + r" sha=([0-9a-f]{7,40}) id=("
                        + _NAME + r") -->\s*$")


def _defang(text):
    """No HTML comments in session text: the marker below is how we recognise
    our own issues, so session text must not be able to forge one."""
    return text.replace("<!--", "<!- -")


def known_issues(ctx):
    """{(sha, name): url} of issues this tool already filed in the repo, found
    by the marker on the LAST line of the body (the issue is the ledger, so a
    crash between creating it and commenting on the commit can't duplicate it)."""
    if "issues" in ctx.cache:
        return ctx.cache["issues"]
    found: dict = {}
    counts: dict = {}
    for page in range(1, MAX_LEDGER_PAGES + 1):
        items = ctx.api("GET", f"/repos/{ctx.repo}/issues?labels=sandbox"
                        f"&state=all&per_page=100&page={page}") or []
        for it in items:
            if "pull_request" in it:
                continue
            m = _LEDGER_RE.search(it.get("body") or "")
            if m:
                if (m.group(1), m.group(2)) not in found:
                    found[(m.group(1), m.group(2))] = it["html_url"]
                    for lab in it.get("labels") or []:
                        name = lab.get("name", "") if isinstance(lab, dict) else lab
                        if name.startswith("session:"):
                            counts[name[8:]] = counts.get(name[8:], 0) + 1
        if len(items) < 100:
            break
    ctx.cache["issues"] = found
    ctx.cache["counts"] = counts
    return found


def session_of(ctx):
    """The session a run is for, if it is one session's (else None)."""
    s = ctx.session if ctx.session != "all" else None
    return s if s and sandbox_names.SESSION_RE.match(s) else None


def ensure_labels(ctx, names):
    """Create missing labels once per run (best effort: see sandbox_labels)."""
    sandbox_labels.ensure(ctx.api, ctx.repo, names,
                          ctx.cache.setdefault("labels", set()))


def fetch_file(ctx, req):
    """The request's file as bytes at its adding commit, or None if it isn't
    there. Reads one byte past the limit so an oversized file is noticed."""
    path = urllib.parse.quote(req.path)
    try:
        raw = ctx.api("GET", f"/repos/{ctx.repo}/contents/{path}?ref={req.sha}",
                      raw=True)
    except RuntimeError as e:
        if getattr(e, "code", None) == 404:
            return None
        raise
    return raw[:sandbox_todo.MAX_BYTES + 1] if raw else None


def _issue(ctx, req):
    raw = fetch_file(ctx, req)
    if raw is None:
        raise Rejected("no such file at that commit")
    if len(raw) > sandbox_todo.MAX_BYTES:
        raise PolicyRejected(f"not filed: {req.path} is over "
                             f"{sandbox_todo.MAX_BYTES} bytes")
    text = raw.decode("utf-8", errors="replace")
    title = sandbox_todo.title(text)
    if not title or not clean_line(title, MAX_TITLE):
        raise Rejected("a request needs a heading")
    hits = sandbox_untrusted.find_secrets(text)
    if hits:
        raise PolicyRejected(f"not filed: {req.path} appears to contain "
                             + ", ".join(hits))
    name = name_of(req)
    existing = known_issues(ctx).get((req.sha, name))
    if existing:
        return Reused(existing)
    session = session_of(ctx)
    cap = limit(ctx, "issues")
    have = ctx.cache["counts"].get(session, 0) if session else 0
    if session and have >= cap:
        raise PolicyRejected(
            f"not filed: session {session} already has {have} issues here "
            f"(cap {cap}; the launcher's --max-issues raises it)")
    labels = [sandbox_labels.SANDBOX, sandbox_labels.FOLLOW_UP] + (
        [sandbox_labels.session_label(session)] if session else [])
    ensure_labels(ctx, labels)
    body = (f"{_defang(clean_block(text, MAX_BODY))}\n\n"
            f"[`{req.path}` at that commit](https://github.com/{ctx.repo}"
            f"/blob/{req.sha}/{req.path})")
    text = (sandbox_templates.render(
        "issue.md", commit=req.sha, session=session or ctx.session, item=name,
        body=body) + f"\n<!-- {MARKER} sha={req.sha} id={name} -->")
    issue = ctx.api("POST", f"/repos/{ctx.repo}/issues",
                    {"title": clean_line(title, MAX_TITLE), "body": text,
                     "labels": labels})
    ctx.cache["issues"][(req.sha, name)] = issue["html_url"]
    if session:
        ctx.cache["counts"][session] = have + 1
    return issue["html_url"]


# --- idempotency ledger: a marker comment on the commit -------------------- #
_DONE_RE = re.compile(r"<!-- " + MARKER + r"-done id=(" + _NAME + r") -->")


def done_names(api, repo, sha):
    """Names already carried out for this commit (read from its comments)."""
    comments = api("GET", f"/repos/{repo}/commits/{sha}/comments?per_page=100") or []
    return {m.group(1) for c in comments
            for m in _DONE_RE.finditer(c.get("body") or "")}


def mark_done(api, repo, req, detail):
    """Cross-link: comment on the commit with the result. This is also the
    ledger that makes a re-run skip the request."""
    api("POST", f"/repos/{repo}/commits/{req.sha}/comments", {"body": (
        f"Follow-up `{req.path}` filed by claude_sandbox: {detail}\n"
        f"<!-- {MARKER}-done id={name_of(req)} -->")})


def execute(ctx, requests):
    """Carry out requests -> [Outcome]. One failure never stops the rest."""
    if ctx.cache is None:
        ctx = ctx._replace(cache={})
    out = []
    done = {}
    for req in requests[:limit(ctx, "actions")]:
        try:
            if req.sha not in done:
                done[req.sha] = done_names(ctx.api, ctx.repo, req.sha)
            if name_of(req) in done[req.sha]:
                out.append(Outcome(req.sha, req.path, "exists", ""))
                continue
            detail = _issue(ctx, req)
            mark_done(ctx.api, ctx.repo, req, detail)
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


# --- wiring ---------------------------------------------------------------- #
def session_commit_files(api, repo, default, branches):
    """[(sha, [added paths])] of commits the branches add over the default
    branch, oldest first within a branch, shas de-duplicated."""
    def q(s):
        return urllib.parse.quote(s, safe="/")
    out, seen = [], set()
    for b in branches:
        cmp_ = api("GET", f"/repos/{repo}/compare/{q(default)}...{q(b)}") or {}
        for c in (cmp_.get("commits") or [])[:MAX_COMMITS_SCANNED]:
            if c["sha"] in seen:
                continue
            seen.add(c["sha"])
            full = api("GET", f"/repos/{repo}/commits/{c['sha']}") or {}
            out.append((c["sha"], [f["filename"] for f in full.get("files") or []
                                   if f.get("status") == "added"]))
    return out


def run(repo, session=None, api=None, limits=None):
    """Find and file the requests in a session's (or every session's, if
    None) unmerged commits -> [Outcome]."""
    import sandbox_github
    import sandbox_land
    api = api or sandbox_github.gh_api
    default = api("GET", f"/repos/{repo}")["default_branch"]
    branches = sandbox_land.session_branches(repo, session, api)
    requests = requests_from(session_commit_files(api, repo, default, branches))
    return execute(Context(repo, session or "all", branches, api, None, limits),
                   requests)


def format_outcomes(outcomes):
    """Exit-summary lines (plain text); empty if nothing was requested."""
    if not outcomes:
        return ""
    label = {"done": "filed", "exists": "already done", "failed": "FAILED",
             "rejected": "REJECTED (policy)"}
    lines = ["follow-up issues"]
    for o in outcomes:
        lines.append(f"  {label[o.status]:<18} {o.sha[:7]}:{o.path}"
                     + (f"  {o.detail}" if o.detail else ""))
    return "\n".join(lines)
