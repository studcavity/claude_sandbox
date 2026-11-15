"""Tests for sandbox_protect and the launcher's protection prompt."""
import json
import os

import pytest

import claude_sandbox as cs
import sandbox_protect as sp
from sandbox_github import GitHubError


@pytest.fixture
def base(repo_root):
    return sp.load_ruleset(os.path.join(repo_root, "rulesets",
                                        "protect-main.json"))


def test_with_checks_adds_strict_required_rule_without_mutating(base):
    n = len(base["rules"])
    rs = sp.with_checks(base, ["test (3.12)", "lint"])
    assert len(base["rules"]) == n  # original untouched
    rule = rs["rules"][-1]
    assert rule["type"] == "required_status_checks"
    assert rule["parameters"]["strict_required_status_checks_policy"] is True
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "lint"}, {"context": "test (3.12)"}]
    assert sp.with_checks(base, []) == base


def test_discover_checks_only_successes_deduped(monkeypatch):
    monkeypatch.setattr(sp, "gh_api", lambda *a, **k: {"check_runs": [
        {"name": "test (3.12)", "conclusion": "success"},
        {"name": "test (3.12)", "conclusion": "success"},
        {"name": "flaky", "conclusion": "failure"},
        {"name": "running", "conclusion": None}]})
    assert sp.discover_checks("o/r", "main") == ["test (3.12)"]


def test_discover_checks_is_best_effort(monkeypatch):
    def boom(*a, **k):
        raise GitHubError("x", 403)
    monkeypatch.setattr(sp, "gh_api", boom)
    assert sp.discover_checks("o/r", "main") == []


def test_apply_posts_the_ruleset(monkeypatch, base):
    seen = []
    monkeypatch.setattr(sp, "gh_api", lambda m, p, body=None, **k:
                        seen.append((m, p, body)) or {"id": 9})
    assert sp.apply_ruleset("o/r", base) == 9
    assert seen == [("POST", "/repos/o/r/rulesets", base)]


# -- launcher flow --------------------------------------------------------- #
@pytest.fixture
def flow(make_args, monkeypatch):
    """Sandbox.protect_default_branch with a scripted GitHub and terminal."""
    st = {"state": "unprotected", "applied": [], "asked": [], "answers": [],
          "existing": set(), "checks": [], "repo_rulesets": ([], []),
          "fetch_args": []}
    box = cs.Sandbox(make_args(repo="o/r", no_protect_prompt=False))
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cs.sandbox_sessions, "check_protection",
                        lambda repo: ("main", "WARN" if st["state"] !=
                                      "protected" else None, st["state"]))
    monkeypatch.setattr(cs.sandbox_protect, "existing_ruleset_names",
                        lambda r: st["existing"])
    monkeypatch.setattr(cs.sandbox_protect, "fetch_repo_rulesets",
                        lambda r, b: st["fetch_args"].append((r, b))
                        or st["repo_rulesets"])
    monkeypatch.setattr(cs.sandbox_protect, "discover_checks",
                        lambda r, b: st["checks"])
    monkeypatch.setattr(cs.sandbox_protect, "apply_ruleset",
                        lambda r, rs: st["applied"].append(rs) or 1)

    def answer(*a):
        st["asked"].append(a)
        return st["answers"].pop(0)
    monkeypatch.setattr(cs, "ask_protect", lambda *a: answer("protect"))
    monkeypatch.setattr(cs, "ask_checks", lambda *a: answer("checks"))
    monkeypatch.setattr(cs, "ask_ruleset", lambda *a: answer(("ruleset", a[1])))
    return box, st


def test_declined_applies_nothing_and_note_says_unprotected(flow):
    box, st = flow
    st["answers"] = [False]
    box.protect_default_branch()
    assert st["applied"] == []
    assert "does **not** require pull requests" in box.protection_note


def test_yes_applies_ruleset_without_checks_by_default(flow):
    box, st = flow
    st["answers"] = [True]
    box.protect_default_branch()
    (rs,) = st["applied"]
    assert "required_status_checks" not in {r["type"] for r in rs["rules"]}
    assert "No status checks are required yet" in box.protection_note


def test_yes_to_checks_requires_the_discovered_names(flow):
    box, st = flow
    st["checks"], st["answers"] = ["test (3.12)"], [True, True]
    box.protect_default_branch()
    (rs,) = st["applied"]
    assert rs["rules"][-1]["parameters"]["required_status_checks"] == [
        {"context": "test (3.12)"}]
    assert "`test (3.12)`" in box.protection_note


def test_no_to_checks_still_applies_base_ruleset(flow):
    box, st = flow
    st["checks"], st["answers"] = ["test (3.12)"], [True, False]
    box.protect_default_branch()
    assert "required_status_checks" not in {
        r["type"] for r in st["applied"][0]["rules"]}


def test_never_prompts_when_protected_or_flagged_or_not_a_tty(flow,
                                                              monkeypatch):
    box, st = flow
    st["state"] = "protected"
    box.protect_default_branch()
    assert st["asked"] == [] and "already requires" in box.protection_note

    st["state"] = "unprotected"
    box.args.no_protect_prompt = True
    box.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []

    box.args.no_protect_prompt = False
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    box.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []


def test_unknown_state_is_not_offered_a_change(flow):
    box, st = flow
    st["state"] = "unknown"
    box.protect_default_branch()
    assert st["asked"] == [] and "could not be verified" in box.protection_note


def test_existing_same_named_ruleset_is_not_duplicated(flow):
    box, st = flow
    st["existing"] = {"protect-default-branch"}
    box.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []


def test_apply_failure_never_aborts_and_note_stays_honest(flow, monkeypatch):
    box, st = flow
    st["answers"] = [True]

    def boom(r, rs):
        raise GitHubError("403 plan has no rulesets", 403)
    monkeypatch.setattr(cs.sandbox_protect, "apply_ruleset", boom)
    box.protect_default_branch()  # must not raise
    assert "does **not** require pull requests" in box.protection_note


def test_protection_note_wording():
    n = cs.protection_note("o/r", "main", "protected", ["lint"])
    assert "`lint`" in n and "o/r" in n
    assert "none known" in cs.protection_note("o/r", "main", "protected")


def test_template_covers_stacking_and_status_checks(repo_root):
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    md = cs.render_claude_md(
        t, {"sudo": True, "tools": [], "branch": "", "usb": set()},
        "quirky-hopper", "/h/r", "o/r", False, "", session="quirky-hopper",
        protection_note=cs.protection_note("o/r", "main", "applied", ["lint"]))
    assert "Stacking PRs is fine" in md and "sbx/quirky-hopper/<next>" in md
    assert "only ever stack on your own" in md.lower()
    assert "required_status_checks" in md and "gh api repos/o/r/rulesets" in md
    assert "`lint`" in md and "(`o/r`)" in md
    assert not __import__("re").search(r"\$[a-z_{]", md)


def test_ruleset_roundtrips_as_json(base):
    json.dumps(sp.with_checks(base, ["x"]))


# -- rulesets defined by the repo itself ------------------------------------ #
def repo_rs(**kw):
    rs = {"name": "require-x", "target": "branch", "enforcement": "active",
          "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"],
                                      "exclude": []}},
          "bypass_actors": [],
          "rules": [{"type": "required_status_checks", "parameters": {
              "required_status_checks": [{"context": "ci"}]}}]}
    rs.update(kw)
    return rs


def test_valid_repo_ruleset_is_normalised_and_input_untouched():
    src = repo_rs()
    del src["bypass_actors"]
    out = sp.validate_repo_ruleset(src)
    assert out["bypass_actors"] == [] and "bypass_actors" not in src


@pytest.mark.parametrize("bad", [
    [], "x", None, {**repo_rs(), "extra": 1},
    repo_rs(name=""), repo_rs(name="a/b"), repo_rs(name=5),
    repo_rs(target="tag"), repo_rs(enforcement="disabled"),
    repo_rs(bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole"}]),
    repo_rs(conditions={"ref_name": {"include": ["refs/heads/*"],
                                     "exclude": []}}),
    repo_rs(conditions={"ref_name": {"include": ["~DEFAULT_BRANCH"],
                                     "exclude": ["main"]}}),
    repo_rs(conditions={"ref_name": {"include": ["~DEFAULT_BRANCH"],
                                     "exclude": []}, "repository_name": {}}),
    repo_rs(conditions=[]), repo_rs(conditions=None),
    repo_rs(rules=[]), repo_rs(rules=[{"type": "workflows"}]),
    repo_rs(rules=[{"type": "deletion", "x": 1}]),
    repo_rs(rules=[{"type": "deletion", "parameters": []}]),
    repo_rs(rules="deletion"), repo_rs(rules=["deletion"]),
])
def test_unsafe_or_malformed_repo_rulesets_are_rejected(bad):
    with pytest.raises(ValueError):
        sp.validate_repo_ruleset(bad)


class Contents:
    """Fake contents API for rulesets/: {name: (size, text)}."""
    def __init__(self, files, listing_error=None):
        self.files, self.error, self.calls = files, listing_error, []

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append(path)
        if self.error:
            raise self.error
        if path.startswith("/repos/o/r/contents/rulesets?"):
            return [{"type": "file", "name": n, "size": sz}
                    for n, (sz, _) in self.files.items()] + [
                {"type": "dir", "name": "sub.json"}]
        name = path.split("/rulesets/")[1].split("?")[0]
        return self.files[name][1].encode()


def test_fetch_reads_default_branch_and_skips_bad_files(monkeypatch):
    good = json.dumps(repo_rs())
    api = Contents({"ok.json": (len(good), good),
                    "bad.json": (3, "[1]"),
                    "evil.json": (10, json.dumps(repo_rs(bypass_actors=[1]))),
                    "junk.json": (4, "nope"),
                    "big.json": (sp.MAX_RULESET_BYTES + 1, "{}"),
                    "notes.md": (1, "x"), ".hidden.json": (1, "{}")})
    monkeypatch.setattr(sp, "gh_api", api)
    found, problems = sp.fetch_repo_rulesets("o/r", "my branch")
    assert [r["name"] for r in found] == ["require-x"]
    assert len(problems) == 4 and all("rulesets/" in p for p in problems)
    assert all("ref=my%20branch" in c for c in api.calls)
    assert not any("notes.md" in c or "hidden" in c or "big.json" in c
                   for c in api.calls[1:])


def test_fetch_no_directory_is_normal_other_errors_raise(monkeypatch):
    monkeypatch.setattr(sp, "gh_api", Contents({}, GitHubError("nf", 404)))
    assert sp.fetch_repo_rulesets("o/r", "main") == ([], [])
    monkeypatch.setattr(sp, "gh_api", Contents({}, GitHubError("no", 500)))
    with pytest.raises(GitHubError):
        sp.fetch_repo_rulesets("o/r", "main")


def test_fetch_caps_the_number_of_files(monkeypatch):
    t = json.dumps(repo_rs())
    api = Contents({f"r{i}.json": (len(t), t) for i in range(15)})
    monkeypatch.setattr(sp, "gh_api", api)
    found, problems = sp.fetch_repo_rulesets("o/r", "main")
    assert len(found) == sp.MAX_REPO_RULESETS and "15 ruleset files" in problems[0]


def test_pending_required_checks_and_description():
    a, b = repo_rs(), repo_rs(name="other")
    assert sp.pending([a, b], {"require-x"}) == [b]
    assert sp.required_checks(a) == ["ci"]
    text = "\n".join(sp.describe_rules(a, "main"))
    assert "`ci`" in text and "no bypass actors" in text
    both = repo_rs(rules=[{"type": "pull_request"}, {"type": "deletion"}],
                   enforcement="evaluate")
    text = "\n".join(sp.describe_rules(both, "main"))
    assert "pull request" in text and "deletion" in text and "evaluate" in text


def test_repo_defined_rulesets_replace_the_generic_one(flow):
    box, st = flow
    st["repo_rulesets"] = ([repo_rs()], [])
    st["answers"] = [True]
    box.protect_default_branch()
    (rs,) = st["applied"]
    assert rs["name"] == "require-x"          # not protect-default-branch
    assert st["asked"] == [(("ruleset", "require-x"),)]  # one question only
    assert "`ci`" in box.protection_note
    assert "No status checks" not in box.protection_note


def test_repo_ruleset_is_offered_even_on_a_protected_branch(flow):
    box, st = flow
    st["state"] = "protected"
    st["repo_rulesets"] = ([repo_rs()], [])
    st["answers"] = [True]
    box.protect_default_branch()
    assert len(st["applied"]) == 1
    assert "already requires pull requests" in box.protection_note
    assert "`ci`" in box.protection_note


def test_declined_and_existing_repo_rulesets_apply_nothing(flow):
    box, st = flow
    st["state"] = "protected"
    st["repo_rulesets"] = ([repo_rs()], [])
    st["answers"] = [False]
    box.protect_default_branch()
    assert st["applied"] == [] and len(st["asked"]) == 1
    st["existing"], st["asked"] = {"require-x"}, []
    box.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []


def test_protected_repo_with_no_repo_rulesets_is_never_prompted(flow):
    box, st = flow
    st["state"] = "protected"
    box.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []
    assert st["fetch_args"] == [("o/r", "main")]


def test_each_repo_ruleset_is_asked_separately(flow):
    box, st = flow
    st["repo_rulesets"] = ([repo_rs(name="one"), repo_rs(name="two")], [])
    st["answers"] = [True, False]
    box.protect_default_branch()
    assert [r["name"] for r in st["applied"]] == ["one"]
    assert len(st["asked"]) == 2


def test_repo_rulesets_are_never_fetched_without_a_prompt(flow, monkeypatch):
    box, st = flow
    box.args.no_protect_prompt = True
    box.protect_default_branch()
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    box.args.no_protect_prompt = False
    box.protect_default_branch()
    assert st["fetch_args"] == []


def test_this_repos_own_rulesets_are_valid_and_their_checks_exist(repo_root):
    """Cross-reference: every ruleset we ship passes the same validation a
    repo-defined one must, and every check it requires is a real CI job here
    (a required check that never reports blocks every merge)."""
    import glob
    import yaml
    jobs = set()
    for wf in glob.glob(os.path.join(repo_root, ".github", "workflows", "*.yml")):
        for key, job in yaml.safe_load(open(wf))["jobs"].items():
            jobs.add(job.get("name", key))
    required = set()
    for path in glob.glob(os.path.join(repo_root, "rulesets", "*.json")):
        rs = sp.validate_repo_ruleset(sp.load_ruleset(path))
        required.update(sp.required_checks(rs))
    import sandbox_check
    import sandbox_provenance
    assert "commit-metadata" in required
    # sandbox/check and sandbox/provenance are commit STATUSES the launcher
    # posts, not CI jobs
    statuses = {sandbox_check.STATUS_CONTEXT, sandbox_provenance.CONTEXT}
    assert statuses <= required
    assert required - statuses <= jobs


# --- rulesets/optional: opt-in, applied by hand ---------------------------- #
OPTIONAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "rulesets", "optional")
ADMIN_BYPASS = [{"actor_id": 5, "actor_type": "RepositoryRole",
                 "bypass_mode": "always"}]


def _optional(name):
    with open(os.path.join(OPTIONAL, name)) as f:
        return json.load(f)


def test_branch_ruleset_confines_non_admins_to_sandbox_branches():
    """Deploy keys aren't admins, so with this a session can only create,
    update or delete branches under sbx/ (no gh-pages, no release branch)."""
    rs = _optional("sandbox-branches-only.json")
    assert rs["target"] == "branch" and rs["enforcement"] == "active"
    assert rs["conditions"]["ref_name"] == {"include": ["~ALL"],
                                            "exclude": ["refs/heads/sbx/**"]}
    assert rs["bypass_actors"] == ADMIN_BYPASS   # never DeployKey
    assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion"}


def test_tag_ruleset_stops_non_admins_pushing_tags():
    """A pushed tag runs tag-triggered (release) workflows on code the
    session wrote, with whatever publishing secrets they use."""
    rs = _optional("no-tag-pushes.json")
    assert rs["target"] == "tag" and rs["bypass_actors"] == ADMIN_BYPASS
    assert rs["conditions"]["ref_name"]["include"] == ["~ALL"]
    assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion"}


def test_optional_rulesets_are_never_offered_automatically():
    """They carry a bypass actor, which repo-supplied rulesets must not: the
    launcher's validator refuses them, and they live in a subdirectory the
    launcher's listing skips."""
    for name in os.listdir(OPTIONAL):
        with pytest.raises(ValueError):
            sp.validate_repo_ruleset(_optional(name))


def test_required_actions_checks_are_pinned_to_github_actions():
    """Without integration_id, any app or token that can post a status named
    `commit-metadata` satisfies the requirement."""
    root = os.path.dirname(OPTIONAL)
    with open(os.path.join(root, "require-commit-metadata.json")) as f:
        rs = json.load(f)
    (rule,) = rs["rules"]
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "commit-metadata", "integration_id": 15368}]
