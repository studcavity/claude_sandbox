"""github/protect.py - apply the default-branch ruleset to a repo (host only).

Offered at launch when the default branch doesn't require pull requests. It
changes the repo's GitHub settings, so it only ever runs after an explicit yes
at a terminal (or never, with --no-protect-prompt); nothing here is automatic.

The base ruleset is rulesets/protect-main.json. Required status checks are
repo-specific, so they are added only when the user opts in, using check names
that actually reported *success* on the default branch's head commit; a
required check that never reports would block every merge.

A repo can also define its own rulesets, in the same place: `rulesets/*.json`
on its default branch (the reviewed, merged state; never a session's branch).
When it does, those are offered *instead of* the generic one above, each only
after a yes at the terminal. Their content is untrusted text that edits repo
settings, so it is held to a strict allowlist (`validate_repo_ruleset`): the
default branch only, no bypass actors, known rule types, known keys. Anything
else is skipped with a warning. Rulesets layer in GitHub, so a repo-defined one
can hold just the extra rule (e.g. one required check) next to the base.
"""
import copy
import json
import re
import urllib.parse

from claude_sandbox.github.gh import GitHubError, gh_api


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


def rule_checks(rules):
    """Check names the effective rules of a branch require (the list GitHub
    returns for `/rules/branches/<branch>`)."""
    return {c["context"] for r in rules or []
            if r.get("type") == "required_status_checks"
            for c in (r.get("parameters") or {}).get("required_status_checks")
            or [] if isinstance(c, dict) and c.get("context")}


def classic_checks(protection):
    """Check names a classic branch protection requires (its `contexts` and
    `checks`)."""
    rsc = (protection or {}).get("required_status_checks") or {}
    names = set(rsc.get("contexts") or [])
    names.update(c["context"] for c in rsc.get("checks") or []
                 if isinstance(c, dict) and c.get("context"))
    return names


def required_on(repo, branch):
    """Check names required to merge into `branch` (rulesets and classic
    protection together), or None if it can't be read."""
    quoted = urllib.parse.quote(branch, safe="")
    try:
        names = rule_checks(gh_api("GET", f"/repos/{repo}/rules/branches/{quoted}"))
    except GitHubError:
        return None
    try:
        names |= classic_checks(gh_api(
            "GET", f"/repos/{repo}/branches/{quoted}/protection"))
    except GitHubError as e:
        if e.code != 404:
            return None
    return names


def gap_warnings(repo, branch, state, required, ci):
    """Plain-text warnings, FOR THE USER, about gaps in `branch`'s protection.

    state: protected | applied | unprotected | unknown. required: the check
    names the branch requires, or None when that couldn't be read. ci: names
    that passed on its head commit. These go to the host's terminal at launch
    and exit and into nothing a session can read: a list of a repo's weak
    points is a map for a prompt-injected session."""
    out = []
    if state == "unprotected":
        out.append(f"`{branch}` of {repo} does not require pull requests: no "
                   "ruleset enforces the PR workflow, so nothing stops a "
                   "direct push (launch on a terminal to be offered "
                   "rulesets/protect-main.json)")
    elif state == "unknown":
        out.append(f"couldn't verify whether `{branch}` of {repo} is protected")
    if state in ("protected", "applied") and required is not None:
        ci, missing = sorted(ci), sorted(set(ci) - required)
        how = (" To require them, add a required_status_checks rule to the "
               f"ruleset (`gh api repos/{repo}/rulesets` lists its id; README: "
               "Required status checks).")
        if not required:
            out.append(f"no status check is required to merge into `{branch}` "
                       f"of {repo}"
                       + (": CI jobs that passed there are "
                          + ", ".join(f"`{c}`" for c in ci) + "." + how
                          if ci else ""))
        elif missing:
            out.append(f"CI jobs not required to merge into `{branch}` of "
                       f"{repo}: " + ", ".join(f"`{c}`" for c in missing)
                       + "." + how)
    return out


def apply_ruleset(repo, ruleset):
    """Create the ruleset; returns its id. Raises GitHubError (e.g. 403/422 when
    the account's plan has no rulesets for this repo, or no admin)."""
    return gh_api("POST", f"/repos/{repo}/rulesets", ruleset)["id"]


def describe(ruleset, branch):
    """Plain-language effect, shown before asking."""
    return [f"require a pull request to change `{branch}` (no direct pushes)",
            "block force-pushes and deletion of it",
            "no bypass actors: this includes you, so you will merge through "
            "PRs too (edit or delete the ruleset in Settings -> Rules to undo)"]


# --- rulesets defined by the repo itself (rulesets/*.json) ------------------ #
RULESET_DIR = "rulesets"
MAX_REPO_RULESETS = 10
MAX_RULESET_BYTES = 20_000
RULESET_KEYS = {"name", "target", "enforcement", "conditions", "bypass_actors",
                "rules"}
RULE_TYPES = {"deletion", "non_fast_forward", "pull_request",
              "required_status_checks", "required_signatures"}
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json$")


def validate_repo_ruleset(data):
    """A repo-supplied ruleset -> a normalised copy, or ValueError.

    Allowed: only the default branch, enforcement active|evaluate, no bypass
    actors (the copy always has none), known top-level keys and rule types.
    """
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    extra = set(data) - RULESET_KEYS
    if extra:
        raise ValueError(f"unknown keys {sorted(extra)}")
    name = data.get("name")
    if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9][\w .()-]{0,99}$",
                                                 name):
        raise ValueError("needs a plain `name` (letters, digits, space, ._-())")
    if data.get("target") != "branch":
        raise ValueError("`target` must be \"branch\"")
    if data.get("enforcement") not in ("active", "evaluate"):
        raise ValueError("`enforcement` must be active or evaluate")
    if data.get("bypass_actors") not in (None, []):
        raise ValueError("`bypass_actors` must be empty")
    cond = data.get("conditions")
    ref = cond.get("ref_name") if isinstance(cond, dict) else None
    if (not isinstance(cond, dict) or not isinstance(ref, dict)
            or set(cond) != {"ref_name"}
            or ref.get("include") != ["~DEFAULT_BRANCH"]
            or ref.get("exclude") != []):
        raise ValueError("`conditions` must be exactly ref_name including "
                         "only ~DEFAULT_BRANCH and excluding nothing")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("needs at least one rule")
    for r in rules:
        if (not isinstance(r, dict) or r.get("type") not in RULE_TYPES
                or set(r) - {"type", "parameters"}
                or not isinstance(r.get("parameters", {}), dict)):
            raise ValueError(f"unsupported rule {str(r)[:60]!r}; allowed "
                             f"types: {sorted(RULE_TYPES)}")
    out = copy.deepcopy(data)
    out["bypass_actors"] = []
    return out


def fetch_repo_rulesets(repo, branch):
    """-> (valid rulesets, problems) from `rulesets/*.json` on `branch`.

    No such directory is normal (`([], [])`). Bounded in count and size.
    Problems are human-readable strings (one per skipped file)."""
    ref = urllib.parse.quote(branch, safe="")
    try:
        listing = gh_api("GET", f"/repos/{repo}/contents/{RULESET_DIR}?ref={ref}")
    except GitHubError as e:
        if e.code == 404:
            return [], []
        raise
    if not isinstance(listing, list):
        return [], []
    found, problems = [], []
    files = [f for f in listing if isinstance(f, dict) and f.get("type") == "file"
             and _FILE_RE.match(str(f.get("name")))]
    if len(files) > MAX_REPO_RULESETS:
        problems.append(f"{len(files)} ruleset files; only the first "
                        f"{MAX_REPO_RULESETS} are read")
    for f in files[:MAX_REPO_RULESETS]:
        label = f"{RULESET_DIR}/{f['name']}"
        if f.get("size", 0) > MAX_RULESET_BYTES:
            problems.append(f"{label}: larger than {MAX_RULESET_BYTES} bytes")
            continue
        try:
            raw = gh_api("GET", f"/repos/{repo}/contents/{label}?ref={ref}",
                         raw=True)
            found.append(validate_repo_ruleset(json.loads(raw)))
        except (ValueError, GitHubError) as e:
            problems.append(f"{label}: {e}")
    return found, problems


def pending(rulesets, existing_names):
    """The rulesets whose names the repo doesn't have yet."""
    return [r for r in rulesets if r["name"] not in existing_names]


def required_checks(ruleset):
    """Check names the ruleset requires."""
    return sorted(c["context"] for r in ruleset["rules"]
                  if r["type"] == "required_status_checks"
                  for c in r.get("parameters", {}).get(
                      "required_status_checks", []))


def describe_rules(ruleset, branch):
    """Plain-language effect of any (validated) ruleset, shown before asking."""
    said = {
        "deletion": f"block deletion of `{branch}`",
        "non_fast_forward": f"block force-pushes to `{branch}`",
        "pull_request": f"require a pull request to change `{branch}` "
                        "(no direct pushes)",
        "required_signatures": "require signed commits",
    }
    out = [said[r["type"]] for r in ruleset["rules"] if r["type"] in said]
    checks = required_checks(ruleset)
    if checks:
        out.append("require these status checks to pass: "
                   + ", ".join(f"`{c}`" for c in checks)
                   + " (a name that never reports would block every merge)")
    if ruleset["enforcement"] == "evaluate":
        out.append("only evaluate (report), not enforce")
    out.append("no bypass actors: this includes you (edit or delete the "
               "ruleset in Settings -> Rules to undo)")
    return out
