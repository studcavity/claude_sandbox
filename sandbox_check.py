"""sandbox_check.py - lint the work record of a range of commits (CI).

    python sandbox_check.py BASE HEAD [--branch REF] [--strict]
    python sandbox_check.py BASE HEAD --post-status OWNER/REPO [--strict]
    python sandbox_check.py --post-status OWNER/REPO --pr N [--strict]
    python sandbox_check.py --post-status OWNER/REPO --pr N --override REASON
    python sandbox_check.py --file-issues OWNER/REPO [--since REV | --last N] [--dry-run]

Prints GitHub annotations (`::error::`, `::warning::`, `::notice::`) for every
commit in BASE..HEAD that breaks a rule, and exits 1 if there is any error
(with --strict, any warning too). If REF is `sbx/<session>/...` every action
id must name that session. The follow-up-metadata
rules live in `sandbox_actions.lint_commit`; the rest are below. This file is
git plumbing plus small pure rules, and is stdlib-only.

--post-status: after checking, post the result as the commit status
`sandbox/check` on the head commit (needs only the user's `gh` login and its
`repo` scope), so a ruleset can require it with no workflow in the repo. With
--pr the PR's base/head/branch come from GitHub and its commits are fetched
into a throwaway repository; the head is re-read afterwards and nothing is
posted if it moved. A crashed checker posts `error`, never `success`.
--override REASON (with --pr) is the audited break-glass path from
docs/plans/README.md: it posts `success` for that one SHA with an
`OVERRIDDEN by <login>: <reason>` description and comments on the PR. Verified
against real GitHub on 2026-11-15 (scripts/probe_commit_status.sh).

--file-issues: the post-merge filer. The launcher files `Sandbox-Action` issues
when a session ends, from its UNMERGED branches. Commits that reach the default
branch without that run (launcher crashed, --no-actions, merged first, pushed
by other tooling) are never filed; this scans commits already on the current
branch (run it in a checkout of the repo) with the same parser, handlers,
sanitising, secret scan, labels and dedupe as the launcher, so running it twice
or alongside the launcher is safe. --dry-run lets reads through and records,
but never performs, a write.

Rules and severities
  error  merge commit (history must stay linear)
  error  `fixup!` / `squash!` / `amend!` left in a subject
  error  a commit touching a file that needs a real-environment run (see
         NEEDS_REAL_RUN) with no `Verified` / `Not verified` line (a `:` is optional)
  error  `Resolves follow-up from <sha>` naming a commit that doesn't exist
  error  a credential-shaped string in a commit message, or in a line the
         commit ADDS (checked per commit, so a secret added and later removed
         is still caught: it stays in history)
  warn   subject isn't `type: summary` (conventional commits)
  warn   source files changed but no test file changed anywhere in the range
  notice a secret finding suppressed by an allow marker (below)

Secret allow marker. A line that deliberately contains a fake credential can
say `secret-scan: allow` (any comment syntax) and is reported as a NOTICE
instead of an error. The marker lives in the very change being judged, so it
is in-band: it can't stop a careless leak from being *noticed* (every use
prints a notice with file and line for the reviewer) but it is not a defence
against a session that wants to hide one. Prefer assembling fixtures at
runtime (`"ghp_" + "x" * 40`) so nothing token-shaped is in the source at all.
Commit messages have no marker: rephrase them.

Everything read here was written by an AI session. Findings quote at most our
own text (rule names, ids, paths) and never a matched secret, and annotation
syntax in that text is escaped so a commit message can't forge `::error::`.
"""
import argparse
import collections
import re
import subprocess
import sys
import urllib.parse

import sandbox_actions
import sandbox_labels
import sandbox_names
import sandbox_untrusted

Finding = collections.namedtuple("Finding", "sha severity text path line")
ERROR, WARNING, NOTICE = "error", "warning", "notice"

CONVENTIONAL = re.compile(
    r"^(?:feat|fix|docs|test|tests|ci|chore|refactor|perf|build|style|revert|wip)"
    r"(?:\([^)\s]+\))?!?: \S")
FIXUP = re.compile(r"^(?:fixup|squash|amend)! ")
# CLAUDE.md allows prose ("Not verified against real incus"), not only `Key:`.
VERIFIED = re.compile(r"(?im)^\s*(?:not\s+verified|verified)\b")
RESOLVES = re.compile(r"(?i)\bResolves follow-up from ([0-9a-f]{7,40})\b")
ALLOW_MARKER = "secret-scan: allow"
# Changes to these need a run in the real environment, so the commit must say
# whether that happened (CLAUDE.md "Rules"). Repo-specific by nature.
NEEDS_REAL_RUN = frozenset({"sandbox_containers.py", "sandbox_images.py",
                            "sandbox_deps.py"})
SOURCE_SUFFIXES = (".py", ".sh", ".js", ".ts", ".go", ".rs", ".rb", ".java",
                   ".c", ".cc", ".cpp", ".h")
MAX_ADDED_LINES = 50000   # per commit; beyond this the scan says it stopped
MAX_SCAN_CHARS = 20000    # per line


def git(*args, cwd=None):
    # errors="replace": a hostile or merely non-UTF-8 message must not crash CI.
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, errors="replace", cwd=cwd).stdout


def branch_session(ref):
    """'sbx/<session>/x' (or refs/heads/...) -> session, else None."""
    parts = (ref or "").removeprefix("refs/heads/").split("/")
    if len(parts) >= 3 and parts[0] == sandbox_names.BRANCH_ROOT:
        return parts[1]
    return None


# --- pure rules ------------------------------------------------------------ #
def subject_findings(message):
    subject = (message.splitlines() or [""])[0]
    out = []
    if FIXUP.match(subject):
        out.append((ERROR, "leftover fixup!/squash!/amend! commit: fold it in "
                           "before merging"))
    elif not (CONVENTIONAL.match(subject) or subject.startswith("Revert ")):
        out.append((WARNING, "subject isn't `type: summary` "
                             "(feat, fix, docs, test, ci, chore, refactor, ...)"))
    return out


def verification_findings(message, files):
    touched = sorted(f for f in files if f.rsplit("/", 1)[-1] in NEEDS_REAL_RUN)
    if touched and not VERIFIED.search(message):
        return [(ERROR, f"touches {', '.join(touched)}, which needs a real "
                        "run: add a `Verified:` or `Not verified:` line")]
    return []


def resolves_findings(message, exists):
    return [(ERROR, f"`Resolves follow-up from {ref}`: no such commit")
            for ref in dict.fromkeys(RESOLVES.findall(message))
            if not exists(ref)]


def message_secret_findings(message):
    kinds = sandbox_untrusted.find_secrets(message[:MAX_SCAN_CHARS * 5])
    return [(ERROR, "the commit message appears to contain "
                    + ", ".join(kinds) + " (no allow marker applies to "
                    "messages; rephrase it)")] if kinds else []


def parse_added_lines(diff):
    """Unified diff (-U0) -> [(path, new line number, text)] for added lines."""
    out: list = []
    path, line = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].split("\t")[0].strip('"')
            path = None if p == "/dev/null" else p.removeprefix("b/")
        elif raw.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path is not None:
            out.append((path, line, raw[1:]))
            line += 1
    return out


def added_secret_findings(added):
    """-> [(severity, text, path, line)] for a commit's added lines."""
    out = []
    for n, (path, line, text) in enumerate(added):
        if n >= MAX_ADDED_LINES:
            out.append((WARNING, f"secret scan stopped after {MAX_ADDED_LINES} "
                                 "added lines in this commit", None, None))
            break
        kinds = sandbox_untrusted.find_secrets(text[:MAX_SCAN_CHARS])
        if not kinds:
            continue
        if ALLOW_MARKER in text:
            out.append((NOTICE, "secret-scan allowed here ("
                        + ", ".join(kinds) + "): reviewer, confirm this is a "
                        "fixture", path, line))
        else:
            out.append((ERROR, "added line appears to contain "
                        + ", ".join(kinds), path, line))
    return out


def is_test_path(path):
    parts = path.split("/")
    name = parts[-1]
    return (any(p in ("tests", "test", "__tests__") for p in parts[:-1])
            or name.startswith("test_") or name.endswith(("_test.py", ".test.js",
                                                         ".test.ts", "_test.go")))


def test_coverage_finding(files):
    src = sorted(f for f in files if f.endswith(SOURCE_SUFFIXES)
                 and not is_test_path(f))
    if src and not any(is_test_path(f) for f in files):
        return (WARNING, "source changed but no test file changed in this "
                         f"range ({', '.join(src[:3])}{', ...' if len(src) > 3 else ''})")
    return None


# --- git plumbing ---------------------------------------------------------- #
def commit_exists(ref, cwd=None):
    return subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"],
                          capture_output=True, cwd=cwd).returncode == 0


def check_findings(base, head, branch=None, exists=None, cwd=None):
    """-> [Finding] for every commit in base..head (merges included), reading
    the repository in `cwd` (default: the current directory)."""
    for r in (base, head):
        if r.startswith("-"):
            raise ValueError(f"not a revision: {r!r}")
    exists = exists or (lambda ref: commit_exists(ref, cwd))

    def git(*args):  # this repo, not whatever the process happens to be in
        return globals()["git"](*args, cwd=cwd)
    session = branch_session(branch)
    out: list = []
    all_files: set = set()

    def add(sha, found):
        out.extend(Finding(sha[:7], sev, text, None, None) for sev, text in found)

    for sha in git("rev-list", "--reverse", f"{base}..{head}").split():
        parents = git("rev-list", "--parents", "-n", "1", sha).split()[1:]
        if len(parents) > 1:
            add(sha, [(ERROR, "merge commit: history must stay linear "
                              "(rebase instead of merging)")])
            continue
        message = git("show", "-s", "--format=%B", sha)
        names = git("diff-tree", "-r", "-z", "--no-commit-id", "--name-only",
                    "--diff-filter=AM", "--no-renames", "--root", sha)
        files = {n for n in names.split("\0") if n}
        all_files |= files
        add(sha, [(ERROR, p) for p in
                  sandbox_actions.lint_commit(message, files, session)])
        add(sha, subject_findings(message))
        add(sha, verification_findings(message, files))
        add(sha, resolves_findings(message, exists))
        add(sha, message_secret_findings(message))
        diff = git("diff-tree", "-p", "-U0", "--no-color", "--no-ext-diff",
                   "--no-textconv", "-r", "--no-commit-id", "--diff-filter=AM",
                   "--no-renames", "--root", sha)
        out.extend(Finding(sha[:7], sev, text, path, line) for sev, text, path,
                   line in added_secret_findings(parse_added_lines(diff)))
    cov = test_coverage_finding(all_files)
    if cov:
        out.append(Finding("", cov[0], cov[1], None, None))
    return out


def check_range(base, head, branch=None):
    """-> [(short sha, problem)]: the errors only (the original contract)."""
    return [(f.sha, f.text) for f in check_findings(base, head, branch)
            if f.severity == ERROR]

# --- commit status (the merge gate) ----------------------------------------- #
STATUS_CONTEXT = "sandbox/check"   # what a ruleset requires; keep in sync with
STATUS_DESC_MAX = 140              # rulesets/require-sandbox-check.json (tested)
MIN_OVERRIDE_REASON = 10
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class CheckError(ValueError):
    """A refusal to post, with a message meant for the person running us."""


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def status_for(findings, strict=False):
    """-> (state, description) for the commit status."""
    errors = sum(f.severity == ERROR for f in findings)
    warnings = sum(f.severity == WARNING for f in findings)
    bad = errors or (strict and warnings)
    desc = f"{_plural(errors, 'error')}, {_plural(warnings, 'warning')}"
    return ("failure" if bad else "success"), desc


def override_description(login, reason):
    """The status description for a break-glass override (<= 140 chars)."""
    reason = " ".join(sandbox_untrusted.printable(reason).split())
    if len(reason) < MIN_OVERRIDE_REASON:
        raise CheckError(f"--override needs a real reason (>= "
                         f"{MIN_OVERRIDE_REASON} characters)")
    if sandbox_untrusted.find_secrets(reason):
        raise CheckError("--override reason looks like it contains a "
                         "credential; not posted")
    who = login if LOGIN_RE.match(login or "") else "unknown"
    return sandbox_untrusted.truncate(f"OVERRIDDEN by {who}: {reason}",
                                      STATUS_DESC_MAX)


def override_comment(login, reason, sha, findings):
    """PR comment that makes an override auditable. `findings` is None when the
    checker itself failed."""
    who = login if LOGIN_RE.match(login or "") else "unknown"
    lines = [f"**`{STATUS_CONTEXT}` overridden** by `{who}` for commit "
             f"`{sha[:7]}`.", "",
             "Reason: " + sandbox_untrusted.one_line(reason, 500), ""]
    if findings is None:
        lines.append("The checker itself failed, so nothing was checked.")
    else:
        bad = [f for f in findings if f.severity in (ERROR, WARNING)]
        lines.append(f"Findings overridden: {_plural(sum(f.severity == ERROR for f in bad), 'error')}, "
                     f"{_plural(sum(f.severity == WARNING for f in bad), 'warning')}.")
        lines += [f"- {f.severity}: " + sandbox_untrusted.one_line(
                      (f"{f.sha}: " if f.sha else "") + f.text, 200)
                  for f in bad[:20]]
        if len(bad) > 20:
            lines.append(f"- ... and {len(bad) - 20} more")
    lines += ["", "This applies to this commit only: a new commit needs a new "
                  "check (or a new override)."]
    return "\n".join(lines)


def pr_details(client, repo, number):
    """-> (base sha, head sha, head branch, html url) of a PR from this
    repository (a fork's commits aren't ours to bless)."""
    pr = client.pr(repo, number)
    if (pr.head_repo or "").lower() != repo.lower():
        raise CheckError(f"PR #{number} comes from {pr.head_repo or 'a fork'}, "
                         f"not {repo}: not posting a status for someone "
                         "else's commits")
    return pr.base_sha, pr.head_sha, pr.head_ref, pr.url


def fetch_pr(url, number, base_sha, dest):
    """Fetch a PR's head and its base commit into a fresh bare repository at
    `dest`, authenticating through the user's gh login (no token handled)."""
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha or ""):
        raise CheckError(f"unexpected base commit id {base_sha!r}")
    try:
        git("init", "-q", "--bare", dest)
        git("-C", dest, "-c", "credential.helper=",
            "-c", "credential.helper=!gh auth git-credential",
            "fetch", "-q", "--no-tags", url,
            f"+refs/pull/{int(number)}/head:refs/pr/head", base_sha)
    except subprocess.CalledProcessError as e:
        raise CheckError(f"couldn't fetch PR #{int(number)} from {url}: "
                         f"{(e.stderr or '').strip()[-200:]}") from None


def sync_check_label(client, repo, pr, state):
    """`check-failed` on the PR while its status is red, removed when it turns
    green (re-run or override). Best effort and never fatal: the status is the
    gate, the label is a convenience."""
    name = sandbox_labels.CHECK_FAILED
    try:
        if state in ("failure", "error"):
            color, desc = sandbox_labels.colour_and_description(name)
            client.ensure_label(repo, name, color, desc)
            client.add_label(repo, pr, name)
        else:
            client.remove_label(repo, pr, name)
    except RuntimeError:
        pass  # refused, or not there to remove: neither matters


def post_status_run(repo, pr=None, base=None, head=None, branch=None,
                    override=None, strict=False, client=None, url=None,
                    out=print, result=None):
    """Check a range or a PR and post `sandbox/check`. -> exit code:
    0 success posted, 1 failure/refusal, 2 the checker crashed (`error` posted).
    If `result` (a dict) is given it receives the posted state/description/sha.
    """
    import tempfile
    import sandbox_github
    sandbox_github.validate_repo(repo)
    url = url or f"https://github.com/{repo}.git"
    if override and not pr:
        raise CheckError("--override needs --pr: an override applies to one "
                         "PR's head commit")
    if client is None:
        import sandbox_ghclient
        client = sandbox_ghclient.GhClient.from_environment()
    target = None
    with tempfile.TemporaryDirectory() as tmp:
        cwd = None
        if pr:
            base, sha, branch, target = pr_details(client, repo, pr)
            fetch_pr(url, pr, base, tmp)
            cwd, head = tmp, sha
        else:
            sha = git("rev-parse", "--verify", f"{head}^{{commit}}").strip()
        login = None
        if override:
            login = client.login()
            desc_override = override_description(login, override)  # validates now
        try:
            findings = check_findings(base, sha, branch, cwd=cwd)
            crashed = False
        except Exception as e:  # a broken checker must never read as success
            out(f"::error::the checker crashed: {type(e).__name__}")
            findings, crashed = None, True
    for f in findings or []:
        out(annotation(f))
    if override:
        state, desc = "success", desc_override
    elif crashed:
        state, desc = "error", "check crashed: see the log"
    else:
        state, desc = status_for(findings, strict)
    if pr and pr_details(client, repo, pr)[1] != sha:
        raise CheckError(f"PR #{pr} head moved while checking; nothing "
                         "posted. Re-run.")
    try:
        client.post_status(repo, sha, state, STATUS_CONTEXT, desc, target)
        if override:
            client.comment(repo, pr, override_comment(login, override, sha, findings))
    except RuntimeError as e:
        raise CheckError(f"couldn't post the status: {e}") from None
    if pr:
        sync_check_label(client, repo, pr, state)
    out(f"posted {STATUS_CONTEXT} {state} on {sha[:7]}: {desc}")
    if result is not None:
        result.update(state=state, description=desc, sha=sha)
    return 2 if crashed and not override else (0 if state == "success" else 1)

# --- gating a session's PRs (launcher wiring, plan 5d) ---------------------- #
StatusOutcome = collections.namedtuple("StatusOutcome", "pr state detail")


def gate_required(api, repo):
    """Does the default branch's effective ruleset require `sandbox/check`?
    The launcher only posts the status where the repo opted in this way."""
    default = api("GET", f"/repos/{repo}")["default_branch"]
    rules = api("GET", f"/repos/{repo}/rules/branches/"
                f"{urllib.parse.quote(default, safe='')}") or []
    return any(r.get("type") == "required_status_checks"
               and any(c.get("context") == STATUS_CONTEXT for c in
                       (r.get("parameters") or {}).get("required_status_checks")
                       or [])
               for r in rules)


def pr_numbers(landed):
    """PR numbers of the open PRs a landing produced (duck-typed on
    `.outcome` and `.detail`, which holds the PR URL for opened/exists, so this
    module needn't import sandbox_land)."""
    nums = []
    for l in landed:
        tail = (l.detail or "").rstrip("/").rsplit("/", 1)[-1]
        if l.outcome in ("opened", "exists") and tail.isdigit():
            nums.append(int(tail))
    return nums


def post_session_statuses(repo, landed, api=None, poster=None, client=None):
    """Post `sandbox/check` for each PR a session opened, if the repo requires
    it -> [StatusOutcome]. One PR failing never affects the others."""
    import sandbox_github
    api = api or sandbox_github.gh_api
    nums = pr_numbers(landed)
    if not nums or not gate_required(api, repo):
        return []
    poster = poster or post_status_run
    out = []
    for n in nums:
        res: dict = {}
        try:
            poster(repo, pr=n, client=client, out=lambda s: None, result=res)
            out.append(StatusOutcome(n, res.get("state", "?"),
                                     res.get("description", "")))
        except Exception as e:  # CheckError, GitHub or git trouble: report, go on
            out.append(StatusOutcome(n, "failed", str(e)[:200]))
    return out


def format_status_outcomes(outcomes):
    if not outcomes:
        return ""
    lines = [f"{STATUS_CONTEXT} status"]
    for o in outcomes:
        lines.append(f"  #{o.pr}: {o.state}" + (f" ({o.detail})" if o.detail else ""))
        if o.state in ("failed", "error", "failure"):
            lines.append(f"     fix, or re-run: python sandbox_check.py "
                         f"--post-status OWNER/REPO --pr {o.pr}")
    return "\n".join(lines)

# --- post-merge filing (plan 5e) --------------------------------------------- #
DEFAULT_LAST = 50


def read_commits(since=None, last=DEFAULT_LAST, head="HEAD", cwd=None):
    """[(sha, message)], oldest first, for `since..head` or the last N commits."""
    for r in (since or "", head):
        if r.startswith("-"):
            raise CheckError(f"not a revision: {r!r}")
    args = ["log", "-z", "--reverse", "--format=%H%x1f%B"]
    args += [f"{since}..{head}"] if since else [f"--max-count={int(last)}", head]
    raw = git(*args, cwd=cwd)
    out = []
    for chunk in raw.split("\0"):
        sha, sep, message = chunk.strip("\n").partition("\x1f")
        if sep and re.fullmatch(r"[0-9a-f]{40}", sha):
            out.append((sha, message))
    return out


class DryRunApi:
    """Reads go through; every write is recorded and answered with a stub."""

    def __init__(self, api):
        self.api, self.writes = api, []

    def __call__(self, method, path, body=None, raw=False):
        if method == "GET":
            return self.api(method, path, body, raw=raw)
        self.writes.append((method, path, body))
        return {"html_url": "(dry run: not created)"}


def file_issues_run(repo, since=None, last=DEFAULT_LAST, dry_run=False,
                    api=None, cwd=None, out=print, limits=None):
    """Scan commits for Sandbox-Action trailers and carry them out -> exit code
    (0 ok, 1 if any item failed)."""
    import sandbox_github
    sandbox_github.validate_repo(repo)
    api = api or sandbox_github.gh_api
    if dry_run:
        api = DryRunApi(api)
    actions = sandbox_actions.parse_commits(read_commits(since, last, cwd=cwd))
    outcomes = sandbox_actions.execute(
        sandbox_actions.Context(repo, "all", [], api, None, limits), actions)
    if dry_run:
        for method, path, body in api.writes:
            if path.endswith("/issues") and method == "POST":
                out(f"would file: {body['title']!r} "
                    f"labels={','.join(body['labels'])}")
    text = sandbox_actions.format_outcomes(outcomes)
    out(text or "no Sandbox-Action trailers found in "
        + (f"{since}..HEAD" if since else f"the last {last} commits"))
    return 1 if any(o.status == "failed" for o in outcomes) else 0



# --- output ---------------------------------------------------------------- #
def _escape(s, prop=False):
    """Escape workflow-command syntax so text from a commit can't forge or
    break an annotation."""
    s = str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        s = s.replace(":", "%3A").replace(",", "%2C")
    return "".join(c if c.isprintable() else " " for c in s)


def annotation(f):
    props = ""
    if f.path:
        props = f" file={_escape(f.path, prop=True)}"
        if f.line:
            props += f",line={f.line}"
    text = f"{f.sha}: {f.text}" if f.sha else f.text
    return f"::{f.severity}{props}::{_escape(text)}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base", nargs="?")
    ap.add_argument("head", nargs="?")
    ap.add_argument("--branch", help="head branch name (action ids on "
                    "sbx/<session>/... must name that session)")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as errors")
    ap.add_argument("--post-status", metavar="OWNER/REPO",
                    help=f"also post the result as the `{STATUS_CONTEXT}` "
                    "commit status on the head commit")
    ap.add_argument("--pr", type=int, metavar="N",
                    help="with --post-status: take base/head/branch from this "
                    "PR (no BASE HEAD needed)")
    ap.add_argument("--override", metavar="REASON",
                    help="with --pr: break glass. Post success for this PR's "
                    "head commit only, with the reason, and comment on the PR")
    ap.add_argument("--file-issues", metavar="OWNER/REPO",
                    help="file issues for Sandbox-Action trailers in commits "
                    "on the current branch (post-merge filer)")
    ap.add_argument("--since", metavar="REV",
                    help="with --file-issues: scan REV..HEAD")
    ap.add_argument("--last", type=int, default=DEFAULT_LAST, metavar="N",
                    help=f"with --file-issues: scan the last N commits "
                    f"(default {DEFAULT_LAST}; ignored with --since)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --file-issues: show what would be filed")
    ap.add_argument("--max-actions", type=int, metavar="N",
                    default=sandbox_actions.MAX_ACTIONS,
                    help="with --file-issues: items to carry out (default %(default)s)")
    ap.add_argument("--max-issues", type=int, metavar="N",
                    default=sandbox_actions.MAX_ISSUES_PER_SESSION,
                    help="with --file-issues: issues one session may have "
                    "(default %(default)s)")
    a = ap.parse_args(argv)
    if a.file_issues:
        if a.post_status or a.pr or a.override or a.base or a.head or a.branch:
            ap.error("--file-issues stands alone (use --since/--last/--dry-run)")
        if a.last < 1 or a.max_actions < 1 or a.max_issues < 1:
            ap.error("--last, --max-actions and --max-issues must be at least 1")
        try:
            return file_issues_run(a.file_issues, a.since, a.last, a.dry_run,
                                   limits={"actions": a.max_actions,
                                           "issues": a.max_issues})
        except CheckError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    if (a.since or a.dry_run) and not a.file_issues:
        ap.error("--since and --dry-run need --file-issues OWNER/REPO")
    if (a.pr or a.override) and not a.post_status:
        ap.error("--pr and --override need --post-status OWNER/REPO")
    if a.override and not a.pr:
        ap.error("--override needs --pr N")
    if a.pr and (a.base or a.head or a.branch):
        ap.error("with --pr, BASE/HEAD/--branch come from the PR")
    if not a.pr and not (a.base and a.head):
        ap.error("BASE and HEAD are required (or use --post-status ... --pr N)")
    if a.post_status:
        try:
            return post_status_run(a.post_status, pr=a.pr, base=a.base,
                                   head=a.head, branch=a.branch,
                                   override=a.override, strict=a.strict)
        except CheckError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    findings = check_findings(a.base, a.head, a.branch)
    for f in findings:
        print(annotation(f))
    errors = sum(f.severity == ERROR for f in findings)
    warnings = sum(f.severity == WARNING for f in findings)
    print(f"checked {a.base}..{a.head}: {errors} error(s), {warnings} warning(s)")
    return 1 if errors or (a.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
