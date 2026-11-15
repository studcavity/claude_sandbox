"""sandbox_protect.py - apply the default-branch ruleset to a repo (host only).

Offered at launch when the default branch doesn't require pull requests. It
changes the repo's GitHub settings, so it only ever runs after an explicit yes
at a terminal (or never, with --no-protect-prompt); nothing here is automatic.

The base ruleset is rulesets/protect-main.json. Required status checks are
repo-specific, so they are added only when the user opts in, using check names
that actually reported *success* on the default branch's head commit; a
required check that never reports would block every merge.
"""
import copy
import json
import urllib.parse

from sandbox_github import GitHubError, gh_api


def load_ruleset(path):
    with open(path) as f:
        return json.load(f)


def with_checks(ruleset, checks):
    """Copy of ruleset that also requires `checks` (names) to pass, on a
    branch that is up to date. No checks: unchanged copy."""
    rs = copy.deepcopy(ruleset)
    if checks:
        rs["rules"].append({"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": c} for c in sorted(checks)]}})
    return rs


def existing_ruleset_names(repo):
    """Names of the repo's rulesets (any enforcement)."""
    return {r.get("name") for r in gh_api(
        "GET", f"/repos/{repo}/rulesets?per_page=100") or []}


def discover_checks(repo, branch):
    """Check names that succeeded on the branch's head commit (candidates for
    required_status_checks). Empty on any trouble: this is only a suggestion."""
    try:
        data = gh_api("GET", f"/repos/{repo}/commits/"
                             f"{urllib.parse.quote(branch, safe='')}"
                             "/check-runs?per_page=100")
    except GitHubError:
        return []
    return sorted({c["name"] for c in (data or {}).get("check_runs", [])
                   if c.get("conclusion") == "success"})


def apply_ruleset(repo, ruleset):
    """Create the ruleset; returns its id. Raises GitHubError (e.g. 403/422 when
    the account's plan has no rulesets for this repo, or no admin)."""
    return gh_api("POST", f"/repos/{repo}/rulesets", ruleset)["id"]


def describe(ruleset, branch):
    """Plain-language effect, shown before asking."""
    return [f"require a pull request to change `{branch}` (no direct pushes)",
            "block force-pushes and deletion of it, and require linear history",
            "no bypass actors: this includes you, so you will merge through "
            "PRs too (edit or delete the ruleset in Settings -> Rules to undo)"]
