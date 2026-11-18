"""mergecheck.py - will this session's work land cleanly on its base?

Part of the final check (see `Sandbox.final_check`), run in the live
container after Claude exits. It replaces advice that used to sit in the
session's CLAUDE.md ("check for conflicts before you finish"): doing it here
is deterministic, it covers every branch the launcher will land, and it costs
the session no context unless something is actually wrong.

Which branches, and against what, follows what landing does:

- GitHub: the session's branches *on the remote* (`origin/sbx/<session>/*`,
  after a fetch) are what become PRs, each against the repo's default branch
  (`origin/HEAD`, refreshed from the remote). A local branch that was never
  pushed, or has moved on since, is reported too: it would be lost.
- Local: the session's local branches (`sbx/<session>/*`) are what get
  imported, against the base the session started from (`origin/HEAD`, which
  is the bundle and never moves, so a branch is never behind, only ahead).

Every branch is judged on its own, against the base, the way its PR or
import will be: behind the base, or conflicting with it, is a finding.

`inspect` takes a `git(*args) -> (returncode, stdout)` function, so the
decisions are tested with a fake; `Sandbox.final_check` supplies the real one.
"""
from claude_sandbox.portable.names import BRANCH_ROOT

BASE = "refs/remotes/origin/HEAD"   # the clone's idea of the base, both backends
FETCH_TIMEOUT = "60"


def session_ns(session, remote):
    """The ref namespace holding the session's branches."""
    root = "refs/remotes/origin" if remote else "refs/heads"
    return f"{root}/{BRANCH_ROOT}/{session}/"


def parse_refs(text):
    """`for-each-ref --format='%(refname) %(objectname)'` -> {ref: sha}."""
    out = {}
    for line in (text or "").splitlines():
        ref, _, sha = line.strip().partition(" ")
        if ref and sha:
            out[ref] = sha
    return out


def parse_conflicts(returncode, stdout):
    """`git merge-tree --write-tree --name-only --no-messages` -> the
    conflicted paths ([] when clean), or None when git couldn't say (any exit
    status but 0 and 1)."""
    if returncode == 0:
        return []
    if returncode != 1:
        return None
    lines = stdout.splitlines()[1:]          # first line is the tree id
    paths = []
    for line in lines:
        if not line.strip():
            break
        paths.append(line.strip())
    return paths


def _count(git, *revs):
    rc, out = git("rev-list", "--count", *revs)
    try:
        return int(out.strip()) if rc == 0 else None
    except ValueError:
        return None


def inspect(git, session, remote):
    """-> (findings, notes). `findings`: [(branch, problem)], one problem
    string per way the branch won't land cleanly; `notes`: things the check
    couldn't do (they never fail it). Branch names are without `origin/`."""
    ns = session_ns(session, remote)
    findings, notes = [], []
    if remote:
        rc, _ = git("fetch", "-q", "origin")
        if rc != 0:
            notes.append("couldn't fetch origin: the base below may be stale")
        git("remote", "set-head", "origin", "-a")   # a renamed default branch
    if git("rev-parse", "--verify", "-q", BASE + "^{commit}")[0] != 0:
        return [], notes + ["can't find the base (origin/HEAD): not checked"]
    refs = parse_refs(git("for-each-ref", "--format=%(refname) %(objectname)",
                          ns)[1])
    if remote:
        local = parse_refs(git(
            "for-each-ref", "--format=%(refname) %(objectname)",
            session_ns(session, False))[1])
        pushed = {r.removeprefix("refs/remotes/origin/"): s
                  for r, s in refs.items()}
        for ref, sha in sorted(local.items()):
            name = ref.removeprefix("refs/heads/")
            ahead = _count(git, f"{BASE}..{ref}")
            if not ahead:
                continue          # nothing of its own: nothing to lose
            if name not in pushed:
                findings.append((name, "never pushed (no PR would be opened)"))
            elif pushed[name] != sha:
                findings.append((name, "the local branch differs from the "
                                 "pushed one (push it, or the PR misses it)"))
    for ref in sorted(refs):
        name = ref.removeprefix("refs/remotes/origin/" if remote
                                else "refs/heads/")
        ahead = _count(git, f"{BASE}..{ref}")
        if not ahead:
            continue              # empty, or can't count: no PR/import anyway
        behind = _count(git, f"{ref}..{BASE}")
        rc, out = git("merge-tree", "--write-tree", "--name-only",
                      "--no-messages", BASE, ref)
        conflicts = parse_conflicts(rc, out)
        if conflicts:
            shown = ", ".join(conflicts[:8]) + (" ..." if len(conflicts) > 8 else "")
            findings.append((name, f"conflicts with the base in: {shown}"))
        elif conflicts is None:
            notes.append(f"couldn't test whether {name} merges cleanly")
        if behind:
            findings.append((name, f"{behind} commit(s) behind the base"))
    return findings, notes


def session_branches(git, session):
    """The session's local branches with commits of their own on the base, in
    name order: what the final check's scripts are run on, one by one."""
    refs = parse_refs(git("for-each-ref", "--format=%(refname) %(objectname)",
                          session_ns(session, False))[1])
    return [r.removeprefix("refs/heads/") for r in sorted(refs)
            if _count(git, f"{BASE}..{r}")]


def resume_text(findings, can_push):
    """The part of the resume prompt that describes merge findings."""
    lines = ["## Branches that won't land cleanly",
             "Each branch is judged alone against the base (`origin/HEAD`), "
             "the way its " + ("PR" if can_push else "import") + " will be.",
             ""]
    for branch, problem in findings:
        lines.append(f"- `{branch}`: {problem}")
    lines += ["",
              "For each: `git switch` to it, `git rebase origin/HEAD` "
              "(resolve conflicts, never merge commits), "
              + ("then `git push --force-with-lease` (new commits for anything "
                 "never pushed). " if can_push else "")
              + "A clean rebase isn't proof: the final check re-runs the "
              "tests after you stop, on every branch of the session."]
    return "\n".join(lines) + "\n"
