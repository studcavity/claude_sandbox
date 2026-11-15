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
import collections
import re
import urllib.parse

import sandbox_deps
import sandbox_host
import sandbox_labels
import sandbox_record
import sandbox_templates
import sandbox_untrusted
from sandbox_github import gh_api
from sandbox_sessions import BRANCH_ROOT, branch_prefix

# One row of the exit summary. base is the PR's target (the default branch, or
# the session's own earlier branch when stacked); commits counts what the PR
# adds over that base.
Landed = collections.namedtuple("Landed", "branch base outcome detail commits")
MAX_SIBLINGS = 10  # bound the compare calls used to find a stack parent

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
    return sandbox_untrusted.one_line(text, limit, backticks_to_quotes=True)


MAX_RECORD_ITEMS = 10


def safe_line(text, limit):
    """clean_text, but a line that looks like it holds a credential is replaced
    by a note: commit messages are already in the repo, a PR body is a second
    place to leak the same thing."""
    hits = sandbox_untrusted.find_secrets(text)
    if hits:
        return f"[withheld: appears to contain {', '.join(hits)}]"
    return clean_text(text, limit)


def record_section(rec):
    """Markdown for what the commits say about their own verification and
    follow-ups (see sandbox_record). Every line of session text is sanitised."""
    out = ["### What the commits say", "",
           "_Lifted from the commit messages by the launcher; untrusted like "
           "everything below._", ""]

    def sha(x):
        return x if re.fullmatch(r"[0-9a-f]{4,40}", x or "") else "?"

    def block(title, items, fmt):
        if not items:
            return
        out.append(f"**{title}** ({len(items)})")
        out.extend("- " + fmt(i) for i in items[:MAX_RECORD_ITEMS])
        if len(items) > MAX_RECORD_ITEMS:
            out.append(f"- ...and {len(items) - MAX_RECORD_ITEMS} more")
        out.append("")
    block("Verified", rec.verified, lambda i: f"`{sha(i[0])}` {safe_line(i[1], 160)}")
    block("Not verified", rec.not_verified,
          lambda i: f"`{sha(i[0])}` {safe_line(i[1], 160)}")
    block("Follow-ups", rec.follow_ups,
          lambda i: f"`{sha(i[0])}` {safe_line(i[1], 160)}")
    block("Issues the launcher will file", rec.issues,
          lambda i: f"`{sha(i[0])}` {safe_line(i[2], 120)} "
                    f"(`{safe_line(i[1], 100)}`)")
    if not rec.verified and not rec.not_verified:
        out += ["_No `Verified` / `Not verified` statement in any commit._", ""]
    return "\n".join(out)


def label_pr(repo, pr, session):
    """Label a PR we just opened `sandbox` and `session:<id>`. Best effort: a
    PR without labels is still a PR."""
    tail = (pr.get("html_url") or "").rstrip("/").rsplit("/", 1)[-1]
    number = pr.get("number") or (tail if tail.isdigit() else None)
    names = [sandbox_labels.SANDBOX]
    if sandbox_labels.session_label(session):
        names.append(sandbox_labels.session_label(session))
    if not number:
        return
    try:
        sandbox_labels.ensure(gh_api, repo, names)
        gh_api("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": names})
    except RuntimeError:
        pass


def pr_text(session, branch, subjects, record=None):
    """(title, body) for a session branch from its commit subjects (and, if
    given, the Record of what its commits say about themselves)."""
    tail = branch.split("/", 2)[-1]
    first = clean_text(subjects[0], 80) if subjects else tail
    title = f"[{session}] " + (first if len(subjects) == 1 else
                               f"{clean_text(tail, 40)} ({len(subjects)} commits)")
    shown = "\n".join(safe_line(s, 120) for s in subjects[:MAX_SUBJECTS])
    more = len(subjects) - MAX_SUBJECTS
    body = sandbox_templates.render(
        "pr.md", session=session, branch=clean_text(branch, 120),
        record=(record_section(record) + "\n") if record is not None else "",
        subjects=shown,
        more=f"\n...and {more} more commits.\n" if more > 0 else "")
    return title, body


def _q(s):
    return urllib.parse.quote(s, safe="/")


def session_branches(repo, session=None, api=None):
    """Branch names under sbx/<session>/ (or every session's, if None)."""
    prefix = branch_prefix(session) if session else BRANCH_ROOT + "/"
    refs = (api or gh_api)("GET", f"/repos/{repo}/git/matching-refs/heads/"
                         f"{_q(prefix)}?per_page=100") or []
    names = [r["ref"][len("refs/heads/"):] for r in refs]
    return [n for n in names if n.startswith(prefix)]


def choose_base(default, candidates):
    """Target branch for a PR. candidates are (branch, status, ahead_by) of
    the *same session's* other branches compared to this one: the closest
    strict ancestor becomes the base (a stacked PR whose diff shows only the
    increment); otherwise the default branch."""
    ancestors = [(n, b) for b, status, n in candidates
                 if status == "ahead" and n > 0]
    return min(ancestors)[1] if ancestors else default


def land_branch(repo, branch, default, siblings=(), extra_sensitive=()):
    """Open a PR for one branch -> Landed. Outcomes: 'empty' (nothing ahead
    of the default branch), 'held' (privileged files), 'exists' (open PR url)
    or 'opened' (PR url).

    The hold test always uses the diff against the *default* branch, not the
    stack parent: a stacked PR must not smuggle a privileged change in via the
    branch below it.
    """
    session = branch.split("/")[1] if branch.count("/") >= 2 else "unknown"
    cum = gh_api("GET", f"/repos/{repo}/compare/{_q(default)}...{_q(branch)}")
    if not cum.get("ahead_by"):
        return Landed(branch, default, "empty", "", 0)
    files = [f["filename"] for f in cum.get("files") or []]
    if len(files) >= 300:  # the API truncates; assume the worst
        return Landed(branch, default, "held",
                      "300+ files changed (list truncated)", cum["ahead_by"])
    hits = sensitive_paths(files, extra_sensitive)
    if hits:
        return Landed(branch, default, "held", ", ".join(hits[:5])
                      + (" ..." if len(hits) > 5 else ""), cum["ahead_by"])

    candidates = []
    for sib in sorted(b for b in siblings if b != branch)[:MAX_SIBLINGS]:
        c = gh_api("GET", f"/repos/{repo}/compare/{_q(sib)}...{_q(branch)}")
        candidates.append((sib, c.get("status"), c.get("ahead_by", 0)))
    base = choose_base(default, candidates)
    inc = cum if base == default else gh_api(
        "GET", f"/repos/{repo}/compare/{_q(base)}...{_q(branch)}")

    owner = repo.split("/")[0]
    open_ = gh_api("GET", f"/repos/{repo}/pulls?state=open&per_page=1&head="
                          f"{urllib.parse.quote(owner + ':' + branch, safe='')}")
    if open_:
        return Landed(branch, open_[0].get("base", {}).get("ref", base),
                      "exists", open_[0]["html_url"], inc["ahead_by"])
    subjects = [c["commit"]["message"].split("\n", 1)[0]
                for c in inc.get("commits") or []]
    record = sandbox_record.extract(
        [((c.get("sha") or "")[:7], c["commit"]["message"])
         for c in inc.get("commits") or []])
    title, body = pr_text(session, branch, subjects, record)
    if base != default:
        body += (f"\nStacked on `{clean_text(base, 120)}`: merge that PR "
                 "first. This PR's diff is only what this branch adds.\n")
    pr = gh_api("POST", f"/repos/{repo}/pulls",
                {"title": title, "head": branch, "base": base, "body": body,
                 "maintainer_can_modify": False})
    label_pr(repo, pr, session)
    return Landed(branch, base, "opened", pr["html_url"], inc["ahead_by"])


def land(repo, session=None, extra_sensitive=()):
    """Open PRs for a session's branches (all sessions' if session is None)
    -> [Landed]. A session's branches may stack on each other, never on
    another session's."""
    default = gh_api("GET", f"/repos/{repo}")["default_branch"]
    branches = session_branches(repo, session)
    by_session = collections.defaultdict(list)
    for b in branches:
        by_session[b.split("/")[1]].append(b)
    return [land_branch(repo, b, default, by_session[b.split("/")[1]],
                        extra_sensitive) for b in branches]


def pr_number(url):
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return f"#{tail}" if tail.isdigit() else ""


def format_landing(results, session=None, color=False):
    """The exit summary: one block per branch, stacked PRs marked, plus the
    merge order. Plain text; color only when asked (a terminal)."""
    def paint(code, text):
        return f"\033[{code}m{text}\033[0m" if color else text
    marks = {"opened": ("\u2714", "32", "opened"),
             "exists": ("\u2022", "36", "open"),
             "empty": ("\u00b7", "90", "no change"),
             "held": ("\u26a0", "33", "HELD")}
    title = f"pull requests{f' for session {session}' if session else ''}"
    out = [paint("1", f"\u250c {title} " + "\u2500" * max(3, 56 - len(title)))]
    if not results:
        out.append("\u2502 no session branches were pushed")
    for r in results:
        icon, code, label = marks[r.outcome]
        stacked = r.base.startswith("sbx/")
        head = "\u2502 " + paint(code, f"{icon} {label:<9}")
        if r.outcome in ("opened", "exists"):
            out.append(f"{head} {pr_number(r.detail):<5} {r.branch} "
                       f"\u2192 {r.base}" + (" (stacked)" if stacked else "")
                       + f"  [{r.commits} commit{'s' * (r.commits != 1)}]")
            out.append(f"\u2502 {'':<12}{r.detail}")
        elif r.outcome == "held":
            out.append(f"{head} {r.branch}")
            out.append(f"\u2502 {'':<12}touches {r.detail}; no PR was "
                       "opened. Review the branch by hand.")
        else:
            out.append(f"{head} {r.branch}")
    n = sum(r.outcome == "opened" for r in results)
    foot = (f"{n} opened. " if results else "") + (
        "Merge stacked PRs bottom-up (parents first). " if any(
            r.base.startswith("sbx/") for r in results) else "") \
        + "The launcher never merges."
    out.append("\u2514 " + foot)
    return "\n".join(out)
