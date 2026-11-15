"""github/land.py - turn a session's pushed branches into pull requests.

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

With an interactive `confirm` callback, a hold for *what the branch touches*
becomes a question to the user (default no) instead of a refusal. Holds that
say the check itself can't be trusted (tip can't be pinned, branch moved) are
never overridable.
"""
import collections
import concurrent.futures
import re
import urllib.parse

from claude_sandbox.container import deps as container_deps
from claude_sandbox.container import host
from claude_sandbox.github import labels
from claude_sandbox.github import templates
from claude_sandbox.portable import untrusted
from claude_sandbox.github.gh import GitHubError, gh_api
from claude_sandbox.sessions import BRANCH_ROOT, branch_prefix

# One row of the exit summary. base is the PR's target: always the default
# branch (a PR into another base would get no CI, which is filtered to the
# default branch); commits counts what the PR adds over it.
Landed = collections.namedtuple("Landed", "branch base outcome detail commits")

SENSITIVE_DIRS = (".github/", ".githooks/")
SENSITIVE_FILES = (container_deps.DEPS_FILE, host.HOST_FILE)
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
    return untrusted.one_line(text, limit, backticks_to_quotes=True)


MAX_CAVEATS = 10
MAX_SUMMARY_LINES = 8
MAX_LINES_PER_MESSAGE = 400   # a commit message is read, not parsed forever
_NOT_VERIFIED = re.compile(r"(?i)^\s*not\s+verified\b")
_PR_TITLE = re.compile(r"(?i)^\s*pr-title\s*:\s*(\S.*)$")
_PR_SUMMARY = re.compile(r"(?i)^\s*pr-summary\s*:\s*(\S.*)$")


def safe_line(text, limit):
    """clean_text, but a line that looks like it holds a credential is replaced
    by a note: commit messages are already in the repo, a PR body is a second
    place to leak the same thing."""
    hits = untrusted.find_secrets(text)
    if hits:
        return f"[withheld: appears to contain {', '.join(hits)}]"
    return clean_text(text, limit)


def not_verified_lines(commits):
    """[(short sha, full message)] -> [(short sha, line)]: every line that
    starts with `Not verified`, oldest commit first. These are the caveats a
    reviewer most needs, so they are lifted into the PR body; the rest of what
    a commit says about itself stays in the commit."""
    out: list = []
    for sha, message in commits:
        out.extend((sha, line.strip()) for line in
                   message.splitlines()[:MAX_LINES_PER_MESSAGE]
                   if _NOT_VERIFIED.match(line))
    return out


def synopsis(commits):
    """[(short sha, full message)], oldest first -> None, or (short sha,
    proposed title, [summary lines]) from the newest commit that has a
    `PR-Title:` line; a later commit's synopsis replaces an earlier one."""
    found = None
    for sha, message in commits:
        title: str | None = None
        summary: list[str] = []
        for line in message.splitlines()[:MAX_LINES_PER_MESSAGE]:
            if (m := _PR_TITLE.match(line)):
                title = title or m.group(1).strip()
            elif (m := _PR_SUMMARY.match(line)):
                if len(summary) < MAX_SUMMARY_LINES:
                    summary.append(m.group(1).strip())
        if title:
            found = (sha, title, summary)
    return found


def synopsis_section(syn):
    """Markdown for the session's own proposed title and summary (see
    synopsis). Sanitised like everything else from the session."""
    sha, title, summary = syn
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha or ""):
        sha = "?"
    out = ["### Proposed synopsis", "",
           f"_Proposed by the session in commit `{sha}`; untrusted like "
           "everything below. Its title is this PR's title._",
           "", f"**Proposed title:** {safe_line(title, 120)}", ""]
    out.extend(f"- {safe_line(x, 200)}" for x in summary[:MAX_SUMMARY_LINES])
    return "\n".join(out) + "\n"


def caveats_section(items):
    """Markdown for the `Not verified` lines (see not_verified_lines). Every
    line of session text is sanitised and secret-checked."""
    out: list = ["### Not verified, according to the commits", "",
           "_Lifted from the commit messages by the launcher; untrusted like "
           "everything below._", ""]

    def sha(x):
        return x if re.fullmatch(r"[0-9a-f]{4,40}", x or "") else "?"
    if not items:
        out += ["_No commit says anything is not verified._", ""]
    else:
        out.append(f"({len(items)})")
        out.extend(f"- `{sha(s)}` {safe_line(t, 160)}"
                   for s, t in items[:MAX_CAVEATS])
        if len(items) > MAX_CAVEATS:
            out.append(f"- ...and {len(items) - MAX_CAVEATS} more")
        out.append("")
    return "\n".join(out)


def label_pr(repo, pr, session):
    """Label a PR we just opened `sandbox` and `session:<id>`. Best effort: a
    PR without labels is still a PR."""
    tail = (pr.get("html_url") or "").rstrip("/").rsplit("/", 1)[-1]
    number = pr.get("number") or (tail if tail.isdigit() else None)
    names = [labels.SANDBOX]
    if labels.session_label(session):
        names.append(labels.session_label(session))
    if not number:
        return
    try:
        labels.ensure(gh_api, repo, names)
        gh_api("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": names})
    except RuntimeError:
        pass


def pr_text(session, branch, subjects, caveats=None, syn=None):
    """(title, body) for a session branch from its commit subjects (and, if
    given, its `Not verified` lines: see not_verified_lines). The title is the
    session's proposed one (see synopsis) plus ` [session]` when it proposed
    one, else built from the subjects."""
    tail = branch.split("/", 2)[-1]
    first = clean_text(subjects[0], 80) if subjects else tail
    title = f"[{session}] " + (first if len(subjects) == 1 else
                               f"{clean_text(tail, 40)} ({len(subjects)} commits)")
    if syn:
        # The session's own title, sanitised; the session name goes last so
        # it is the launcher, not the session, that says whose PR this is.
        title = f"{safe_line(syn[1], 100)} [{session}]"
    shown = "\n".join(safe_line(s, 120) for s in subjects[:MAX_SUBJECTS])
    more = len(subjects) - MAX_SUBJECTS
    body = templates.render(
        "pr.md", session=session, branch=clean_text(branch, 120),
        caveats=((synopsis_section(syn) + "\n" if syn else "")
                 + (caveats_section(caveats) + "\n" if caveats is not None else "")),
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
    # Alphanumeric, so "01-x" lands before "02-y" whatever order GitHub lists.
    return sorted(n for n in names if n.startswith(prefix))


def _compare_path(repo, default, branch):
    return f"/repos/{repo}/compare/{_q(default)}...{_q(branch)}"


def _open_pr_path(repo, branch):
    owner = repo.split("/")[0]
    return (f"/repos/{repo}/pulls?state=open&per_page=1&head="
            f"{urllib.parse.quote(owner + ':' + branch, safe='')}")


def land_branch(repo, branch, default, extra_sensitive=(), confirm=None,
                compare=None, open_prs=None):
    """Open a PR for one branch -> Landed. Outcomes: 'empty' (nothing ahead
    of the default branch), 'held' (privileged files), 'exists' (open PR url)
    or 'opened' (PR url).

    Every PR targets the default branch, so a branch built on another of the
    session's branches shows their commits too (and is held if they are).

    `confirm(branch, detail) -> bool`, when given, is asked before a branch
    held for its files is opened anyway; None (non-interactive) keeps the hold.

    `compare` and `open_prs` are optional zero-argument callables returning the
    two read-only GitHub answers this needs (see `land`, which fetches them
    concurrently); by default each is fetched here, when first needed.
    """
    session = branch.split("/")[1] if branch.count("/") >= 2 else "unknown"
    cum = (compare or (lambda: gh_api(
        "GET", _compare_path(repo, default, branch))))()
    if not cum.get("ahead_by"):
        return Landed(branch, default, "empty", "", 0)
    files = [f["filename"] for f in cum.get("files") or []]
    hold = None
    if len(files) >= 300:  # the API truncates; assume the worst
        hold = "300+ files changed (list truncated)"
    else:
        hits = sensitive_paths(files, extra_sensitive)
        if hits:
            hold = ", ".join(hits[:5]) + (" ..." if len(hits) > 5 else "")
    if hold and not (confirm and confirm(branch, hold)):
        return Landed(branch, default, "held", hold, cum["ahead_by"])
    tip = checked_tip(cum)
    if not tip:
        return Landed(branch, default, "held", "couldn't pin the branch tip "
                      "(250+ commits?)", cum["ahead_by"])

    open_ = (open_prs or (lambda: gh_api("GET", _open_pr_path(repo, branch))))()
    if open_:
        return Landed(branch, open_[0].get("base", {}).get("ref", default),
                      "exists", open_[0]["html_url"], cum["ahead_by"])
    subjects = [c["commit"]["message"].split("\n", 1)[0]
                for c in cum.get("commits") or []]
    messages = [((c.get("sha") or "")[:7], c["commit"]["message"])
                for c in cum.get("commits") or []]
    title, body = pr_text(session, branch, subjects, not_verified_lines(messages),
                          synopsis(messages))
    pr = gh_api("POST", f"/repos/{repo}/pulls",
                {"title": title, "head": branch, "base": default, "body": body,
                 "maintainer_can_modify": False})
    moved = head_moved(pr, tip)
    if moved:
        # Another key (a sibling session's) pushed between the check and the
        # PR: what was checked is not what the PR would merge. Close it.
        try:
            gh_api("PATCH", f"/repos/{repo}/pulls/{int(pr['number'])}",
                   {"state": "closed"})
            moved += "; the PR was closed"
        except (RuntimeError, KeyError, TypeError, ValueError):
            moved += f"; CLOSE IT BY HAND: {pr.get('html_url', '?')}"
        return Landed(branch, default, "held", moved, cum["ahead_by"])
    label_pr(repo, pr, session)
    return Landed(branch, default, "opened", pr["html_url"], cum["ahead_by"])


def checked_tip(cmp_):
    """The head commit a `default...branch` compare examined, or None when the
    compare doesn't list every commit (it stops at 250) and so can't say."""
    commits = cmp_.get("commits") or []
    total = cmp_.get("total_commits", len(commits))
    sha = (commits[-1].get("sha") or "") if commits else ""
    if len(commits) < total or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None
    return sha


def head_moved(pr, tip):
    """Why the PR GitHub just opened doesn't point at the commit we checked
    (its head is a branch name, so a push in between moves it), or None.
    Fails closed: no head sha in the response counts as moved."""
    head = (pr.get("head") or {}).get("sha") or ""
    if head == tip:
        return None
    return (f"branch moved while landing (checked {tip[:7]}, the PR got "
            f"{head[:7] or 'an unknown head'})")


def setup_script_paths(repo, default):
    """Repo-relative `setup:` script paths in the default branch's deps file.

    Read here, at landing time, so the review hold doesn't depend on how the
    launcher was run (--land-all and --no-deps never parse the deps file). A
    branch that *adds* a setup entry edits the deps file itself, which is held
    anyway. No file, or one that doesn't parse, means no scripts: an invalid
    file aborts every launch, so nothing in it would run."""
    try:
        raw = gh_api("GET", f"/repos/{repo}/contents/{container_deps.DEPS_FILE}"
                            f"?ref={_q(default)}", raw=True)
        deps = container_deps.parse(raw)
    except GitHubError as e:
        if e.code != 404:
            raise
        return []
    except container_deps.DepsError:
        return []
    return [s["path"] for s in deps.get("setup") or []]


MAX_PARALLEL_READS = 4   # polite to GitHub's secondary rate limits


def land(repo, session=None, confirm=None):
    """Open PRs for a session's branches (all sessions' if session is None)
    -> [Landed]. Each PR targets the default branch.

    The reads are independent, so they run concurrently: first the default
    branch and the branch list, then the deps file and, per branch, its
    compare and its open-PR check. Everything that writes, asks the user
    (`confirm` may read stdin) or decides stays sequential on this thread, in
    branch order. Callers revoke the session's key first, so the branches
    can't move between these reads and the PRs (see head_moved)."""
    with concurrent.futures.ThreadPoolExecutor(MAX_PARALLEL_READS) as pool:
        default_f = pool.submit(gh_api, "GET", f"/repos/{repo}")
        branches = session_branches(repo, session)
        default = default_f.result()["default_branch"]
        extra_f = pool.submit(setup_script_paths, repo, default) if branches else None
        reads = [(pool.submit(gh_api, "GET", _compare_path(repo, default, b)),
                  pool.submit(gh_api, "GET", _open_pr_path(repo, b)))
                 for b in branches]
        extra_sensitive = extra_f.result() if extra_f else []
        return [land_branch(repo, b, default, extra_sensitive, confirm,
                            compare=cmp_f.result, open_prs=open_f.result)
                for b, (cmp_f, open_f) in zip(branches, reads)]


def pr_number(url):
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return f"#{tail}" if tail.isdigit() else ""


def format_landing(results, session=None, color=False):
    """The exit summary: one block per branch. Plain text; color only when asked (a terminal)."""
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
        head = "\u2502 " + paint(code, f"{icon} {label:<9}")
        if r.outcome in ("opened", "exists"):
            out.append(f"{head} {pr_number(r.detail):<5} {r.branch} "
                       f"\u2192 {r.base}"
                       f"  [{r.commits} commit{'s' * (r.commits != 1)}]")
            out.append(f"\u2502 {'':<12}{r.detail}")
        elif r.outcome == "held":
            out.append(f"{head} {r.branch}")
            out.append(f"\u2502 {'':<12}touches {r.detail}; no PR was "
                       "opened. Review the branch by hand.")
        else:
            out.append(f"{head} {r.branch}")
    n = sum(r.outcome == "opened" for r in results)
    foot = (f"{n} opened. " if results else "") + "The launcher never merges."
    out.append("\u2514 " + foot)
    return "\n".join(out)
