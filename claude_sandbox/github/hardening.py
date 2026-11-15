"""github/hardening.py - check (and, on a yes, tighten) the repo's Actions
settings (host only).

THE PRINCIPLE: nothing runs in a trusted execution context before a human has
reviewed it. Session-written code does run in CI (a same-repo PR runs the
branch's own workflow code), and that is fine as long as the run is
*untrusted*: no write access to the repo or to anything that gates a merge, no
secrets it can reach, and logs that stay on GitHub's runner.

GitHub has NO setting that requires approval for runs from same-repo
branches: its approval settings (`fork-pr-contributor-approval`,
`fork-pr-workflows-private-repos`) apply to fork PRs only, and a sandbox PR
comes from a branch in the repo. What can be set deterministically:
  * the default token is read-only
    (`PUT .../actions/permissions/workflow`, `default_workflow_permissions`);
  * Actions may not approve pull requests
    (the same endpoint, `can_approve_pull_request_reviews`).
Each is offered with one yes/no at a terminal (default no), shows exactly the
call it would make, and is idempotent. A 409 (an owning organisation forbids
the change) or 403 (no admin rights) is reported, never fatal.

What can only be DETECTED: repo-level and organisation-level Actions secrets
(names only: values are never readable). The launcher says they exist; it
can't move them. An environment with a protected-branch deployment policy
keeps a secret out of reach of an `sbx/` branch, but the workflow YAML has to
opt in, so no setting does it.

Open question, not settled here (see README): whether a deploy key can push
`.github/workflows/*`. `default_workflow_permissions` is only a default; a
workflow that declares `permissions:` gets what it declares, so if such a push
is accepted these settings do not contain it.

Everything found goes to the USER (launch and exit summary), never into the
sandbox's CLAUDE.md: a list of a repo's weak points is a map for a
prompt-injected session.
"""
import collections

from claude_sandbox.github.gh import GitHubError

MET, UNMET, UNKNOWN = "met", "unmet", "unknown"

# status: met | unmet | unknown; fix: (method, path, body) or None
Condition = collections.namedtuple("Condition", "key status text fix")
# data: the decoded reply, or None; error: the HTTP code (0 = other) or None
Reading = collections.namedtuple("Reading", "data error")


def _get(api, path):
    try:
        return Reading(api("GET", path), None)
    except GitHubError as e:
        return Reading(None, e.code)


def read_settings(repo, api):
    """What the principle depends on, as Readings (a refusal is data, not an
    exception: it becomes `unknown`)."""
    return {
        "workflow": _get(api, f"/repos/{repo}/actions/permissions/workflow"),
        "secrets": _get(api, f"/repos/{repo}/actions/secrets?per_page=1"),
        "org_secrets": _get(
            api, f"/repos/{repo}/actions/organization-secrets?per_page=1"),
    }


def _why(error):
    return {403: "needs repo admin rights", 404: "not available here",
            409: "an organisation policy forbids it"}.get(
                error, f"GitHub answered {error}" if error else "it failed")


def conditions(repo, settings):
    """Settings -> [Condition]: which parts of the principle hold, which don't,
    which can't be read. Pure."""
    wf = settings["workflow"]
    path = f"/repos/{repo}/actions/permissions/workflow"
    out = []
    if wf.error or not isinstance(wf.data, dict):
        out += [Condition("token-read-only", UNKNOWN,
                          f"couldn't read the default token's permissions "
                          f"({_why(wf.error)})", None),
                Condition("actions-cannot-approve", UNKNOWN,
                          f"couldn't read whether Actions may approve PRs "
                          f"({_why(wf.error)})", None)]
    else:
        read_only = wf.data.get("default_workflow_permissions") == "read"
        out.append(Condition(
            "token-read-only", MET if read_only else UNMET,
            "the default workflow token is read-only" if read_only else
            "the default workflow token can write to the repo (a same-repo PR's "
            "workflow code inherits it)",
            None if read_only else
            ("PUT", path, {"default_workflow_permissions": "read"})))
        no_approve = wf.data.get("can_approve_pull_request_reviews") is False
        out.append(Condition(
            "actions-cannot-approve", MET if no_approve else UNMET,
            "Actions may not approve pull requests" if no_approve else
            "Actions may approve pull requests (a workflow could approve its "
            "own session's PR)",
            None if no_approve else
            ("PUT", path, {"can_approve_pull_request_reviews": False})))
    counts, unknown = [], None
    for label, key in (("repo", "secrets"), ("organisation", "org_secrets")):
        r = settings[key]
        if r.error == 404 and key == "org_secrets":
            continue        # a personal repo has no organisation secrets
        if r.error or not isinstance(r.data, dict):
            unknown = unknown or f"couldn't list {label} Actions secrets ({_why(r.error)})"
        elif r.data.get("total_count"):
            counts.append(f"{r.data['total_count']} {label}-level")
    if counts:
        out.append(Condition(
            "no-reachable-secrets", UNMET,
            " and ".join(counts) + " Actions secret(s) exist: any workflow that "
            "runs on a branch a session can push to may read them (the launcher "
            "can't move them; put real secrets in an Environment restricted to "
            "the default branch)", None))
    elif unknown:
        out.append(Condition("no-reachable-secrets", UNKNOWN, unknown, None))
    else:
        out.append(Condition("no-reachable-secrets", MET,
                             "no repo- or organisation-level Actions secrets",
                             None))
    return out


def describe_fix(fix):
    """The exact call an offered change would make, for the prompt."""
    method, path, body = fix
    return f"{method} {path} {body}"


def apply_fix(fix, api):
    """Make the change -> (ok, message). Never raises on GitHub's refusals."""
    method, path, body = fix
    try:
        api(method, path, body)
    except GitHubError as e:
        return False, f"not applied ({_why(e.code)}): {e}"
    return True, "applied"


def harden(repo, api, ask=None):
    """Read, offer each settable change (only if `ask` is given), apply the
    accepted ones, and read again -> ([Condition], [messages]). `ask(condition)
    -> bool`. Idempotent: once applied, a re-run finds nothing to offer."""
    found = conditions(repo, read_settings(repo, api))
    notes = []
    if ask:
        changed = False
        for c in found:
            if c.status == UNMET and c.fix and ask(c):
                ok, msg = apply_fix(c.fix, api)
                notes.append(f"{c.key}: {msg}")
                changed = changed or ok
        if changed:
            found = conditions(repo, read_settings(repo, api))
    return found, notes


def summary_lines(found):
    """One line per condition, for the launch log."""
    mark = {MET: "ok     ", UNMET: "NOT MET", UNKNOWN: "unknown"}
    return [f"{mark[c.status]}  {c.text}" for c in found]


def warnings(found, offered=False):
    """Warnings for the user: every condition that isn't met."""
    out = []
    for c in found:
        if c.status == MET:
            continue
        w = f"Actions: {c.text}"
        if c.status == UNMET and c.fix and not offered:
            w += " (launch on a terminal to be offered the fix)"
        out.append(w)
    return out
