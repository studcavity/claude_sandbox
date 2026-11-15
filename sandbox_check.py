"""sandbox_check.py - lint the follow-up metadata of a range of commits (CI).

    python sandbox_check.py BASE HEAD [--branch REF]

Exit 1 (printing GitHub `::error::` annotations) if any commit in BASE..HEAD
breaks the rules in `sandbox_actions.lint_commit`. If REF is `sbx/<session>/...`
every commit must also carry a matching `Sandbox-Session` trailer. The rules
live in sandbox_actions; this file is only git plumbing.
"""
import argparse
import subprocess
import sys

import sandbox_actions
import sandbox_sessions


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout


def branch_session(ref):
    """'sbx/<session>/x' (or refs/heads/...) -> session, else None."""
    parts = (ref or "").removeprefix("refs/heads/").split("/")
    if len(parts) >= 3 and parts[0] == sandbox_sessions.BRANCH_ROOT:
        return parts[1]
    return None


def check_range(base, head, branch=None):
    """-> [(short sha, problem)] for non-merge commits in base..head."""
    for r in (base, head):
        if r.startswith("-"):
            raise ValueError(f"not a revision: {r!r}")
    session = branch_session(branch)
    out = []
    for sha in git("rev-list", "--reverse", "--no-merges",
                   f"{base}..{head}").split():
        message = git("show", "-s", "--format=%B", sha)
        names = git("diff-tree", "-r", "-z", "--no-commit-id", "--name-only",
                    "--diff-filter=AM", "--no-renames", "--root", sha)
        files = {n for n in names.split("\0") if n}
        out += [(sha[:7], p) for p in
                sandbox_actions.lint_commit(message, files, session)]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("base")
    ap.add_argument("head")
    ap.add_argument("--branch", help="head branch name (decides whether "
                    "Sandbox-Session is required)")
    a = ap.parse_args(argv)
    problems = check_range(a.base, a.head, a.branch)
    for sha, p in problems:
        print(f"::error::{sha}: {p}")
    print(f"checked {a.base}..{a.head}: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
