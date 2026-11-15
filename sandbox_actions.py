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
  id    str matching [A-Za-z0-9._-]{1,100} (required); with the commit sha it
        names the action, so re-running is idempotent. CI requires the shape
        <slug>-<YYYYMMDD>-<session>-<4 random chars>, e.g.
        smoke-20261115-frosty-volta-a3f9, so ids don't collide across commits
        (they name todo/<id>.md); the parser itself accepts any valid id.

Every commit from a session also carries `Sandbox-Session: <session>` in the
same trailer block. CI (`sandbox_check.py`, via `lint_commit`) checks that it
matches the branch (`sbx/<session>/...`) and the session inside each id, and
that every `issue` action has todo/<id>.md in the same commit. At run time the
launcher ignores a commit whose trailer names a different session than the
branch it is on.
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

Convention: an `issue` action's full description lives in the same commit at
`todo/<id>.md`, written so a fresh agent with no context could pick the item
up (CI checks the file exists). When filing, the launcher reads that file AT
THAT COMMIT (so a later deletion doesn't matter), sanitises and bounds it
(MAX_BRIEF), refuses to file if it looks like it holds a credential, appends it
to the issue body with a permalink, and says so if it is missing.

Issues are created with the labels `sandbox`, `follow-up` and
`session:<session>` (created on first use; best effort). The issue is also the
idempotency ledger: its body ends with `<!-- claude-sandbox:action sha=.. id=..
-->`, and before filing the launcher looks for an existing `sandbox`-labelled
issue with that marker on its LAST line, so a crash between creating the issue
and commenting on the commit can't duplicate it. HTML comments in session text
are defanged so a session can't forge a marker. The commit comment remains a
second ledger and is healed when the issue already exists.

Trust: everything here is text written by an AI session. It is bounded
(MAX_*), stripped of control characters and live @-mentions, and labelled
untrusted. Handlers only ever do narrow, reversible, non-merging things.
"""
import collections
import datetime
import json
import re
import urllib.parse

import sandbox_names
import sandbox_untrusted

TRAILER = "Sandbox-Action"
SESSION_TRAILER = "Sandbox-Session"
ID_MAX = 100          # was 40; ids now carry date + session + random suffix
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,%d}$" % ID_MAX)
REF_RE = re.compile(r"^([0-9a-f]{7,40}):([A-Za-z0-9._-]{1,%d})$" % ID_MAX)
SESSION_RE = sandbox_names.SESSION_RE
MAX_ACTIONS = 25      # executed per run, across all commits
MAX_LINE = 8000       # a trailer line longer than this is ignored
MAX_TITLE = 120
MAX_BODY = 4000
MARKER = "claude-sandbox:action"
MAX_BRIEF = 6000          # chars of todo/<id>.md embedded in an issue
MAX_BRIEF_BYTES = 40000   # bytes read from it before decoding
MAX_LEDGER_PAGES = 5      # x100 issues scanned for an existing marker
LABELS = {  # name -> (colour, description); created on first use
    "sandbox": ("5319e7", "Filed by claude_sandbox from a session's commit"),
    "follow-up": ("fbca04", "A follow-up a sandboxed session asked for"),
}

# sha: full commit sha; key: (type, v); data: the whole decoded object.
Action = collections.namedtuple("Action", "sha id type v data")
# status: done | exists | superseded | failed
Outcome = collections.namedtuple("Outcome", "sha id type status detail")


def _trailer_lines(message):
    """Lines of the final paragraph of a commit message, if it is one."""
    paras = [p for p in re.split(r"\n\s*\n", message.strip("\n")) if p.strip()]
    return paras[-1].splitlines() if len(paras) > 1 else []


def scan_message(sha, message):
    """-> (actions, problems). Actions are what the launcher will act on (a
    nonconforming line is simply absent). Problems say why each line that
    starts with the trailer key was dropped, for `lint_commit`; the launcher
    ignores them. Duplicate ids within the commit: the first one wins."""
    prefix = TRAILER.lower() + ":"
    seen, out, problems = set(), [], []
    for line in _trailer_lines(message):
        if not line.lower().startswith(prefix):
            continue
        if len(line) > MAX_LINE:
            problems.append(f"{TRAILER} line longer than {MAX_LINE} chars")
            continue
        try:
            data = json.loads(line[len(prefix):])
        except ValueError:
            problems.append(f"{TRAILER} value is not one line of JSON")
            continue
        if not isinstance(data, dict):
            problems.append(f"{TRAILER} value is not a JSON object")
            continue
        v, typ, aid = data.get("v"), data.get("type"), data.get("id")
        if (type(v) is not int or not isinstance(typ, str)
                or not isinstance(aid, str) or not ID_RE.match(aid)):
            problems.append(f"{TRAILER} needs int v, str type and an id "
                            f"matching {ID_RE.pattern}")
            continue
        if aid in seen:
            problems.append(f"duplicate action id {aid!r}")
            continue
        seen.add(aid)
        out.append(Action(sha, aid, typ, v, data))
    return out, problems


def parse_message(sha, message):
    """Actions requested by one commit message; nonconforming lines dropped."""
    return scan_message(sha, message)[0]


def session_trailers(message):
    """Every `Sandbox-Session:` value (stripped) in the last paragraph."""
    prefix = SESSION_TRAILER.lower() + ":"
    return [ln[len(prefix):].strip() for ln in _trailer_lines(message)
            if ln.lower().startswith(prefix)]


def message_session(message):
    """The session a commit says it came from, or None if it doesn't say
    (or says something invalid or contradictory)."""
    vals = session_trailers(message)
    return vals[0] if len(vals) == 1 and SESSION_RE.match(vals[0]) else None


def id_problem(action_id, session):
    """Why an id isn't `<slug>-<YYYYMMDD>-<session>-<4 chars>`, or None.
    Required by `lint_commit` for new commits; the parser stays lenient
    (old commits are immutable)."""
    m = re.match(r"^[A-Za-z0-9._-]+?-(\d{8})-" + re.escape(session)
                 + r"-[a-z0-9]{4}$", action_id)
    if not m:
        return (f"id {action_id!r} must look like "
                f"<slug>-<YYYYMMDD>-{session}-<4 random [a-z0-9]>")
    try:
        datetime.datetime.strptime(m.group(1), "%Y%m%d")
    except ValueError:
        return f"id {action_id!r} has an impossible date {m.group(1)}"
    return None


def todo_path(action_id):
    return f"todo/{action_id}.md"


def lint_commit(message, files, branch_session=None):
    """Everything wrong with one commit's follow-up metadata -> [str].

    `files`: paths this commit adds or modifies. `branch_session`: the session
    the branch name (`sbx/<session>/...`) belongs to, if it is a session
    branch. Rules, all cross-references of the session name:
      - on a session branch the commit MUST say `Sandbox-Session: <that
        session>`; elsewhere it may, and then must be valid;
      - never more than one Sandbox-Session trailer;
      - every Sandbox-Action line must conform, be a known (type, v), and have
        an id ending in -<date>-<that session>-<4 chars>;
      - every `issue` action needs todo/<id>.md in this same commit.
    """
    actions, problems = scan_message("", message)
    problems = list(problems)
    vals = session_trailers(message)
    session = None
    if len(vals) > 1:
        problems.append(f"{len(vals)} {SESSION_TRAILER} trailers (want one)")
    elif vals and not SESSION_RE.match(vals[0]):
        problems.append(f"invalid {SESSION_TRAILER} {vals[0]!r}")
    elif vals:
        session = vals[0]
    if branch_session and session != branch_session:
        problems.append(
            f"branch belongs to session {branch_session!r} but the commit "
            f"says {SESSION_TRAILER}: {session!r}" if session else
            f"missing '{SESSION_TRAILER}: {branch_session}' trailer")
    for a in actions:
        if (a.type, a.v) not in HANDLERS and (a.type, a.v) not in DIRECTIVES:
            problems.append(f"action {a.id!r}: unknown type/version "
                            f"{a.type!r} v{a.v} (it would be ignored)")
        if not session:
            problems.append(f"action {a.id!r} needs a valid "
                            f"{SESSION_TRAILER} trailer in the same commit")
        elif id_problem(a.id, session):
            problems.append(id_problem(a.id, session))
        if a.type == "issue" and todo_path(a.id) not in files:
            problems.append(f"action {a.id!r}: {todo_path(a.id)} is not "
                            "added or changed in this commit")
    return problems


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
    return sandbox_untrusted.block(text, limit)


def clean_line(text, limit):
    return " ".join(clean_block(text, limit * 2).split())[:limit]


# --- handlers: (type, v) -> function(ctx, action) -> detail str ------------ #
# A handler validates its own payload and raises Rejected (a no-op) when it
# doesn't conform. ctx is a Context; all I/O goes through ctx.api.
class Rejected(ValueError):
    """The payload doesn't conform: a silent no-op, by the spec."""


class PolicyRejected(Rejected):
    """The payload conforms but we refuse to publish it (e.g. it appears to
    contain a credential). Unlike a malformed payload this is reported in the
    exit summary, so the user knows an item was held back and why. The reason
    names the kind of problem, never the offending text."""


# `cache` is per-run scratch (labels ensured, issues already filed); execute()
# supplies it, so callers can keep building Context(repo, session, branches, api).
Context = collections.namedtuple("Context", "repo session branches api cache",
                                 defaults=(None,))


class Reused(str):
    """A handler's detail when the item already existed (e.g. its issue was
    found by marker), so it is reported as "exists", not "done"."""


_LEDGER_RE = re.compile(r"<!-- " + MARKER + r" sha=([0-9a-f]{7,40}) id=("
                        r"[A-Za-z0-9._-]{1,%d}) -->\s*$" % ID_MAX)
_ID_SESSION_RE = re.compile(r"^[A-Za-z0-9._-]+?-\d{8}-(.+)-[a-z0-9]{4}$")


def _defang(text):
    """No HTML comments in session text: the marker below is how we recognise
    our own issues, so session text must not be able to forge one."""
    return text.replace("<!--", "<!- -")


def known_issues(ctx):
    """{(sha, id): url} of issues this tool already filed in the repo, found by
    the marker on the LAST line of the body (the issue is the ledger, so a crash
    between creating it and commenting on the commit can't duplicate it)."""
    if "issues" in ctx.cache:
        return ctx.cache["issues"]
    found: dict = {}
    for page in range(1, MAX_LEDGER_PAGES + 1):
        items = ctx.api("GET", f"/repos/{ctx.repo}/issues?labels=sandbox"
                        f"&state=all&per_page=100&page={page}") or []
        for it in items:
            if "pull_request" in it:
                continue
            m = _LEDGER_RE.search(it.get("body") or "")
            if m:
                found.setdefault((m.group(1), m.group(2)), it["html_url"])
        if len(items) < 100:
            break
    ctx.cache["issues"] = found
    return found


def session_of(ctx, a):
    s = ctx.session if ctx.session != "all" else None
    if not s:
        m = _ID_SESSION_RE.match(a.id)
        s = m.group(1) if m else None
    return s if s and sandbox_names.SESSION_RE.match(s) else None


def ensure_labels(ctx, names):
    """Create missing labels once per run. Best effort: a repo that refuses
    (no push access) still gets the issue, and the commit comment remains a
    second ledger."""
    done = ctx.cache.setdefault("labels", set())
    for name in names:
        if name in done:
            continue
        color, desc = LABELS.get(name, ("ededed", "claude_sandbox session"))
        try:
            ctx.api("POST", f"/repos/{ctx.repo}/labels",
                    {"name": name, "color": color, "description": desc})
        except RuntimeError:
            pass  # already exists (422) or not allowed: either way, carry on
        done.add(name)


def fetch_brief(ctx, a):
    """The commit's todo/<id>.md as text (read at that commit, so a later
    deletion doesn't matter), or None if it isn't there."""
    path = urllib.parse.quote(todo_path(a.id))
    try:
        raw = ctx.api("GET", f"/repos/{ctx.repo}/contents/{path}?ref={a.sha}",
                      raw=True)
    except RuntimeError as e:
        if getattr(e, "code", None) == 404:
            return None
        raise
    return raw[:MAX_BRIEF_BYTES].decode("utf-8", errors="replace") if raw else None


def _issue(ctx, a):
    title, body = a.data.get("title"), a.data.get("body", "")
    if not isinstance(title, str) or not clean_line(title, MAX_TITLE):
        raise Rejected("issue needs a title")
    if not isinstance(body, str):
        raise Rejected("issue body must be a string")
    hits = sandbox_untrusted.find_secrets(f"{title}\n{body}")
    if hits:
        raise PolicyRejected("not filed: the text appears to contain "
                             + ", ".join(hits))
    existing = known_issues(ctx).get((a.sha, a.id))
    if existing:
        return Reused(existing)
    brief = fetch_brief(ctx, a)
    hits = sandbox_untrusted.find_secrets(brief or "")
    if hits:
        raise PolicyRejected(f"not filed: {todo_path(a.id)} appears to contain "
                             + ", ".join(hits))
    if brief is None:
        brief_text = (f"\n\n_No `{todo_path(a.id)}` brief was found at that "
                      "commit._")
    else:
        brief_text = (f"\n\n### Brief (`{todo_path(a.id)}` at `{a.sha[:7]}`)\n\n"
                      f"{_defang(clean_block(brief, MAX_BRIEF))}\n\n"
                      f"[Full file at that commit](https://github.com/{ctx.repo}"
                      f"/blob/{a.sha}/{todo_path(a.id)})")
    session = session_of(ctx, a)
    labels = ["sandbox", "follow-up"] + ([f"session:{session}"] if session else [])
    ensure_labels(ctx, labels)
    text = (
        f"Follow-up requested by commit {a.sha} (session `{session or ctx.session}`), "
        f"item `{a.id}`.\n\n"
        f"{_defang(clean_block(body, MAX_BODY))}{brief_text}\n\n---\n"
        "Filed by claude_sandbox from a `Sandbox-Action` trailer. The text "
        "above was written by an AI session and is untrusted.\n"
        f"<!-- {MARKER} sha={a.sha} id={a.id} -->")
    issue = ctx.api("POST", f"/repos/{ctx.repo}/issues",
                    {"title": clean_line(title, MAX_TITLE), "body": text,
                     "labels": labels})
    ctx.cache["issues"][(a.sha, a.id)] = issue["html_url"]
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
    run: list = []
    skipped: list = []
    for a in actions:
        if (a.type, a.v) not in HANDLERS:
            continue
        (skipped if (a.sha, a.id) in resolved else run).append(a)
    return run[:MAX_ACTIONS], skipped


# --- idempotency ledger: a marker comment on the commit -------------------- #
_DONE_RE = re.compile(r"<!-- " + MARKER + r"-done id=([A-Za-z0-9._-]{1,%d}) -->"
                      % ID_MAX)


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
    if ctx.cache is None:
        ctx = ctx._replace(cache={})
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
            out.append(Outcome(a.sha, a.id, a.type,
                               "exists" if isinstance(detail, Reused) else "done",
                               detail))
        except PolicyRejected as e:
            out.append(Outcome(a.sha, a.id, a.type, "rejected", str(e)))
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
        mine = b.split("/")[1] if b.count("/") >= 2 else None
        cmp_ = api("GET", f"/repos/{repo}/compare/{q(default)}...{q(b)}") or {}
        for c in cmp_.get("commits") or []:
            claimed = message_session(c["commit"]["message"])
            # A commit that claims a different session than its branch is
            # misattributed (or forged): no-op. No claim at all is allowed.
            if claimed in (None, mine):
                out.append((c["sha"], c["commit"]["message"]))
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
             "superseded": "resolved by a later commit", "failed": "FAILED",
             "rejected": "REJECTED (policy)"}
    lines = ["follow-up actions"]
    for o in outcomes:
        lines.append(f"  {label[o.status]:<26} {o.type} {o.sha[:7]}:{o.id}"
                     + (f"  {o.detail}" if o.detail else ""))
    return "\n".join(lines)
