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
          "existing": set(), "checks": []}
    box = cs.Sandbox(make_args(repo="o/r", no_protect_prompt=False))
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cs.sandbox_sessions, "check_protection",
                        lambda repo: ("main", "WARN" if st["state"] !=
                                      "protected" else None, st["state"]))
    monkeypatch.setattr(cs.sandbox_protect, "existing_ruleset_names",
                        lambda r: st["existing"])
    monkeypatch.setattr(cs.sandbox_protect, "discover_checks",
                        lambda r, b: st["checks"])
    monkeypatch.setattr(cs.sandbox_protect, "apply_ruleset",
                        lambda r, rs: st["applied"].append(rs) or 1)

    def answer(*a):
        st["asked"].append(a)
        return st["answers"].pop(0)
    monkeypatch.setattr(cs, "ask_protect", lambda *a: answer("protect"))
    monkeypatch.setattr(cs, "ask_checks", lambda *a: answer("checks"))
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
