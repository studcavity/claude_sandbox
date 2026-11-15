"""sandbox_check.py - lint the work record of a range of commits (CI).

    python sandbox_check.py BASE HEAD [--branch REF] [--strict]
    python sandbox_check.py BASE HEAD --post-status OWNER/REPO [--strict]
    python sandbox_check.py --post-status OWNER/REPO --pr N [--strict]
    python sandbox_check.py --post-status OWNER/REPO --pr N --override REASON

Prints GitHub annotations (`::error::`, `::warning::`, `::notice::`) for every
commit in BASE..HEAD that breaks a rule, and exits 1 if there is any error
(with --strict, any warning too). If REF is `sbx/<session>/...` every commit
must also carry a matching `Sandbox-Session` trailer. The follow-up-metadata
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

import sandbox_actions
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


def pr_info(api, repo, number):
    """-> (base sha, head sha, head branch, html url) of an open PR from this
    repository (a fork's commits aren't ours to bless)."""
    pr = api("GET", f"/repos/{repo}/pulls/{number}")
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
    if head_repo.lower() != repo.lower():
        raise CheckError(f"PR #{number} comes from {head_repo or 'a fork'}, "
                         f"not {repo}: not posting a status for someone "
                         "else's commits")
    return (pr["base"]["sha"], pr["head"]["sha"], pr["head"]["ref"],
            pr.get("html_url"))


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


def post_status_run(repo, pr=None, base=None, head=None, branch=None,
                    override=None, strict=False, api=None, url=None,
                    out=print):
    """Check a range or a PR and post `sandbox/check`. -> exit code:
    0 success posted, 1 failure/refusal, 2 the checker crashed (`error` posted).
    """
    import tempfile
    import sandbox_github
    sandbox_github.validate_repo(repo)
    api = api or sandbox_github.gh_api
    url = url or f"https://github.com/{repo}.git"
    if override and not pr:
        raise CheckError("--override needs --pr: an override applies to one "
                         "PR's head commit")
    target = None
    with tempfile.TemporaryDirectory() as tmp:
        cwd = None
        if pr:
            base, sha, branch, target = pr_info(api, repo, pr)
            fetch_pr(url, pr, base, tmp)
            cwd, head = tmp, sha
        else:
            sha = git("rev-parse", "--verify", f"{head}^{{commit}}").strip()
        login = None
        if override:
            login = api("GET", "/user")["login"]
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
    if pr and pr_info(api, repo, pr)[1] != sha:
        raise CheckError(f"PR #{pr} head moved while checking; nothing "
                         "posted. Re-run.")
    payload = {"state": state, "context": STATUS_CONTEXT, "description": desc}
    if target:
        payload["target_url"] = target
    try:
        api("POST", f"/repos/{repo}/statuses/{sha}", payload)
        if override:
            api("POST", f"/repos/{repo}/issues/{pr}/comments",
                {"body": override_comment(login, override, sha, findings)})
    except RuntimeError as e:
        raise CheckError(f"couldn't post the status: {e}") from None
    out(f"posted {STATUS_CONTEXT} {state} on {sha[:7]}: {desc}")
    return 2 if crashed and not override else (0 if state == "success" else 1)



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
    ap.add_argument("--branch", help="head branch name (decides whether "
                    "Sandbox-Session is required)")
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
    a = ap.parse_args(argv)
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
