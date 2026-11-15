"""sandbox_check.py - lint the work record of a range of commits (CI).

    python sandbox_check.py BASE HEAD [--branch REF] [--strict]

Prints GitHub annotations (`::error::`, `::warning::`, `::notice::`) for every
commit in BASE..HEAD that breaks a rule, and exits 1 if there is any error
(with --strict, any warning too). If REF is `sbx/<session>/...` every commit
must also carry a matching `Sandbox-Session` trailer. The follow-up-metadata
rules live in `sandbox_actions.lint_commit`; the rest are below. This file is
git plumbing plus small pure rules, and is stdlib-only.

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


def git(*args):
    # errors="replace": a hostile or merely non-UTF-8 message must not crash CI.
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, errors="replace").stdout


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
    out, path, line = [], None, 0
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
def commit_exists(ref):
    return subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"],
                          capture_output=True).returncode == 0


def check_findings(base, head, branch=None, exists=commit_exists):
    """-> [Finding] for every commit in base..head (merges included)."""
    for r in (base, head):
        if r.startswith("-"):
            raise ValueError(f"not a revision: {r!r}")
    session = branch_session(branch)
    out, all_files = [], set()

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
    ap.add_argument("base")
    ap.add_argument("head")
    ap.add_argument("--branch", help="head branch name (decides whether "
                    "Sandbox-Session is required)")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as errors")
    a = ap.parse_args(argv)
    findings = check_findings(a.base, a.head, a.branch)
    for f in findings:
        print(annotation(f))
    errors = sum(f.severity == ERROR for f in findings)
    warnings = sum(f.severity == WARNING for f in findings)
    print(f"checked {a.base}..{a.head}: {errors} error(s), {warnings} warning(s)")
    return 1 if errors or (a.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
