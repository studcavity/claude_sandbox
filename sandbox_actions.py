"""sandbox_actions.py - out-of-sandbox actions requested by commit messages.

A sandboxed session can't touch GitHub beyond pushing, but it can say, in a
commit message, "someone outside should do X". The launcher reads those
requests on the host after the session ends and carries out the ones it
understands, with the user's own `gh` login.

Why this exists rather than reusing something: `git interpret-trailers` only
parses `Key: value` lines, it has no notion of versioned, typed, idempotent
requests, and nothing existing turns a commit message into issues. So this is
a small module of our own; the parsing and planning are pure, and only
`execute`/`run` touch GitHub.

THE SPEC (the only thing that is durable; commit messages can't be edited)
-------------------------------------------------------------------------
An action is one git-trailer line in the LAST paragraph of a commit message
(the same block `Co-Authored-By:` lives in):

    Sandbox-Action: {"v": 1, "type": "issue", "id": "smoke", "title": "...", "body": "..."}

The value is ONE line of a JSON object. Envelope, stable forever:
  v     int, the schema version of this action type's payload (required)
  type  str, what to do (required)
  id    str matching [A-Za-z0-9._-]{1,40}, unique within the commit (required);
        with the commit sha it names the action, so re-running is idempotent
Everything else is the payload, owned by (type, v). Unknown keys are ignored.

Anything that does not conform is a NO-OP, never an error: lines outside the
last paragraph, invalid JSON, non-object values, a bad envelope, an unknown
(type, v), a payload the handler rejects, a duplicate id. One bad line never
affects the others. New capabilities are new (type, v) handlers registered in
HANDLERS; old commits keep working because old handlers are never removed.

Types today
  issue v1     title (str, required), body (str, optional): open a GitHub
               issue, then leave a comment on the commit linking to it.
  resolve v1   ref (str, required) "<sha-prefix>:<id>": says this commit
               addressed that earlier action. If both are in the same run the
               earlier one is skipped, so only unaddressed items are filed.
               (An issue that was already filed is left for a human to close.)

Convention (instruction to sessions, not enforced or consumed here yet): an
`issue` action's full description lives in the same commit at `todo/<id>.md`,
written so a fresh agent with no context could pick the item up. It may later
become the issue body; today the launcher ignores it.

Trust: everything here is text written by an AI session. It is bounded
(MAX_*), stripped of control characters and live @-mentions, and labelled
untrusted. Handlers only ever do narrow, reversible, non-merging things.
"""
import collections
import json
import re
import urllib.parse

TRAILER = "Sandbox-Action"
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
REF_RE = re.compile(r"^([0-9a-f]{7,40}):([A-Za-z0-9._-]{1,40})$")
MAX_ACTIONS = 25      # executed per run, across all commits
MAX_LINE = 8000       # a trailer line longer than this is ignored
MAX_TITLE = 120
MAX_BODY = 4000
MARKER = "claude-sandbox:action"

# sha: full commit sha; key: (type, v); data: the whole decoded object.
Action = collections.namedtuple("Action", "sha id type v data")
# status: done | exists | superseded | failed
Outcome = collections.namedtuple("Outcome", "sha id type status detail")


def _trailer_lines(message):
    """Lines of the final paragraph of a commit message, if it is one."""
    paras = [p for p in re.split(r"\n\s*\n", message.strip("\n")) if p.strip()]
    return paras[-1].splitlines() if len(paras) > 1 else []


def parse_message(sha, message):
    """Actions requested by one commit message; nonconforming lines are
    dropped. Duplicate ids within the commit: the first one wins."""
    prefix = TRAILER.lower() + ":"
    seen, out = set(), []
    for line in _trailer_lines(message):
        if not line.lower().startswith(prefix) or len(line) > MAX_LINE:
            continue
        try:
            data = json.loads(line[len(prefix):])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        v, typ, aid = data.get("v"), data.get("type"), data.get("id")
        if (type(v) is not int or not isinstance(typ, str)
                or not isinstance(aid, str) or not ID_RE.match(aid)
                or aid in seen):
            continue
        seen.add(aid)
        out.append(Action(sha, aid, typ, v, data))
    return out


def parse_commits(commits):
    """[(sha, message)] -> [Action], oldest first as given, shas de-duplicated
    (stacked branches share commits)."""
    seen, out = set(), []
    for sha, message in commits:
        if sha in seen:
            continue
        seen.add(sha)
        out.extend(parse_message(sha, message))
    return out


def clean_block(text, limit):
    """Untrusted multi-line text made safe to paste into GitHub: no control
    characters (newlines kept), no live @-mentions, bounded."""
    t = "".join(c if c.isprintable() or c == "\n" else " "
                for c in text.replace("\r", ""))
    t = t.replace("@", "@​").strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


def clean_line(text, limit):
    return " ".join(clean_block(text, limit * 2).split())[:limit]


# --- handlers: (type, v) -> function(ctx, action) -> detail str ------------ #
# A handler validates its own payload and raises Rejected (a no-op) when it
# doesn't conform. ctx is a Context; all I/O goes through ctx.api.
class Rejected(ValueError):
    pass


Context = collections.namedtuple("Context", "repo session branches api")


def _issue(ctx, a):
    title, body = a.data.get("title"), a.data.get("body", "")
    if not isinstance(title, str) or not clean_line(title, MAX_TITLE):
        raise Rejected("issue needs a title")
    if not isinstance(body, str):
        raise Rejected("issue body must be a string")
    text = (
        f"Follow-up requested by commit {a.sha} (session `{ctx.session}`), "
        f"item `{a.id}`.\n\n"
        f"{clean_block(body, MAX_BODY)}\n\n---\n"
        "Filed by claude_sandbox from a `Sandbox-Action` trailer. The text "
        "above was written by an AI session and is untrusted.\n"
        f"<!-- {MARKER} sha={a.sha} id={a.id} -->")
    issue = ctx.api("POST", f"/repos/{ctx.repo}/issues",
                    {"title": clean_line(title, MAX_TITLE), "body": text})
    return issue["html_url"]


HANDLERS = {("issue", 1): _issue}
# Types that only influence planning; they have nothing to execute.
DIRECTIVES = {("resolve", 1)}


def known(a):
    return (a.type, a.v) in HANDLERS or (a.type, a.v) in DIRECTIVES


def _resolved_refs(actions):
    """{(sha, id)} of actions that a conforming resolve directive addresses."""
    full = {a.sha for a in actions}
    out = set()
    for a in actions:
        ref = a.data.get("ref")
        m = REF_RE.match(ref) if (a.type, a.v) == ("resolve", 1) and \
            isinstance(ref, str) else None
        if not m:
            continue
        hits = [s for s in full if s.startswith(m.group(1))]
        if len(hits) == 1 and hits[0] != a.sha:  # ambiguous prefix: no-op
            out.add((hits[0], m.group(2)))
    return out


def plan(actions):
    """-> (to_run, superseded): executable, understood, unaddressed actions
    (capped at MAX_ACTIONS) and those skipped because a later commit
    resolved them."""
    resolved = _resolved_refs(actions)
    run, skipped = [], []
    for a in actions:
        if (a.type, a.v) not in HANDLERS:
            continue
        (skipped if (a.sha, a.id) in resolved else run).append(a)
    return run[:MAX_ACTIONS], skipped


# --- idempotency ledger: a marker comment on the commit -------------------- #
_DONE_RE = re.compile(r"<!-- " + MARKER + r"-done id=([A-Za-z0-9._-]{1,40}) -->")


def done_ids(api, repo, sha):
    """Ids already carried out for this commit (read from its comments)."""
    comments = api("GET", f"/repos/{repo}/commits/{sha}/comments?per_page=100") or []
    return {m.group(1) for c in comments
            for m in _DONE_RE.finditer(c.get("body") or "")}


def mark_done(api, repo, a, detail):
    """Cross-link: comment on the commit with the result. This is also the
    ledger that makes a re-run skip the action."""
    api("POST", f"/repos/{repo}/commits/{a.sha}/comments", {"body": (
        f"`{a.type}` action `{a.id}` carried out by claude_sandbox: {detail}\n"
        f"<!-- {MARKER}-done id={a.id} -->")})


def execute(ctx, actions):
    """Plan and carry out -> [Outcome]. One failure never stops the rest."""
    to_run, skipped = plan(actions)
    out = [Outcome(a.sha, a.id, a.type, "superseded", "") for a in skipped]
    done = {}
    for a in to_run:
        try:
            if a.sha not in done:
                done[a.sha] = done_ids(ctx.api, ctx.repo, a.sha)
            if a.id in done[a.sha]:
                out.append(Outcome(a.sha, a.id, a.type, "exists", ""))
                continue
            detail = HANDLERS[(a.type, a.v)](ctx, a)
            mark_done(ctx.api, ctx.repo, a, detail)
            out.append(Outcome(a.sha, a.id, a.type, "done", detail))
        except Rejected:
            continue  # nonconforming payload: a no-op by spec
        except Exception as e:
            out.append(Outcome(a.sha, a.id, a.type, "failed", str(e)[:200]))
    return out


# --- wiring ---------------------------------------------------------------- #
def session_commits(api, repo, default, branches):
    """[(sha, message)] of commits the branches add over the default branch."""
    def q(s):
        return urllib.parse.quote(s, safe="/")
    out = []
    for b in branches:
        cmp_ = api("GET", f"/repos/{repo}/compare/{q(default)}...{q(b)}") or {}
        out.extend((c["sha"], c["commit"]["message"])
                   for c in cmp_.get("commits") or [])
    return out


def run(repo, session=None, api=None):
    """Find and execute the actions in a session's (or every session's, if
    None) unmerged commits -> [Outcome]."""
    import sandbox_github
    import sandbox_land
    api = api or sandbox_github.gh_api
    default = api("GET", f"/repos/{repo}")["default_branch"]
    branches = sandbox_land.session_branches(repo, session, api)
    actions = parse_commits(session_commits(api, repo, default, branches))
    return execute(Context(repo, session or "all", branches, api), actions)


def format_outcomes(outcomes):
    """Exit-summary lines (plain text); empty if nothing was requested."""
    if not outcomes:
        return ""
    label = {"done": "filed", "exists": "already done",
             "superseded": "resolved by a later commit", "failed": "FAILED"}
    lines = ["follow-up actions"]
    for o in outcomes:
        lines.append(f"  {label[o.status]:<26} {o.type} {o.sha[:7]}:{o.id}"
                     + (f"  {o.detail}" if o.detail else ""))
    return "\n".join(lines)
