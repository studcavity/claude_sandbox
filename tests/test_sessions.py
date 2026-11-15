"""Tests for sessions: session ids, key reaping, protection advice."""
import random

import pytest

from claude_sandbox import sessions as ss
from claude_sandbox.github.gh import GitHubError


def test_pick_name_is_docker_style_and_valid():
    for seed in range(50):
        name = ss.pick_name(rng=random.Random(seed))
        assert ss.validate_session(name) == name
        adj, sur = name.split("-")
        assert adj in ss.ADJECTIVES and sur in ss.SURNAMES
        assert "claude" not in name


def test_pick_name_avoids_taken_and_always_terminates():
    everything = {f"{a}-{s}" for a in ss.ADJECTIVES for s in ss.SURNAMES}
    name = ss.pick_name(taken=everything, rng=random.Random(1))
    assert name not in everything
    assert ss.validate_session(name)  # suffix form still valid


@pytest.mark.parametrize("bad", ["", "-x", "a/b", "a b", "a_b", "x" * 41,
                                 "a..b", "../x", "a;b"])
def test_validate_session_rejects_unsafe(bad):
    with pytest.raises(ValueError):
        ss.validate_session(bad)


def test_branch_names_use_sbx_not_claude():
    assert ss.branch_prefix("quirky-hopper") == "sbx/quirky-hopper/"
    assert ss.work_branch("quirky-hopper") == "sbx/quirky-hopper/work"


def test_host_id_is_key_title_safe():
    assert ss.host_id("my host_1!") == "my-host-1"
    assert ss.host_id("") != ""


def test_stale_keys_only_this_hosts_dead_sessions():
    keys = [
        {"id": 1, "title": "ephemeral h1:dead-one"},      # ours, gone: reap
        {"id": 2, "title": "ephemeral h1:live-one"},      # ours, alive
        {"id": 3, "title": "ephemeral h2:dead-one"},      # other host: keep
        {"id": 4, "title": "ephemeral claude-abcd1234"},  # legacy title: keep
        {"id": 5, "title": "ci deploy key"},              # not ours: keep
        {"id": 6, "title": None},
    ]
    assert ss.stale_keys(keys, "h1", {"live-one"}) == [1]


def test_reap_deletes_only_stale(monkeypatch):
    monkeypatch.setattr(ss, "gh_api", lambda m, p, *a, **k: [
        {"id": 1, "title": "ephemeral h:gone"},
        {"id": 2, "title": "ephemeral h:me"}])
    deleted = []
    assert ss.reap_stale_keys("o/r", "h", {"me"}, delete=deleted.append) == [1]
    assert deleted == [1]


def test_protection_warning_states():
    assert ss.protection_warning("main", {"pull_request"}, False) is None
    assert ss.protection_warning("main", set(), True) is None
    assert "does not require pull requests" in ss.protection_warning(
        "main", {"deletion"}, False)
    assert "couldn't verify" in ss.protection_warning("main", None, None)


def _api(rules=None, protection=None, rules_err=None, prot_err=None):
    def fake(method, path, *a, **k):
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if "/rules/branches/" in path:
            if rules_err:
                raise rules_err
            return rules
        if path.endswith("/protection"):
            if prot_err:
                raise prot_err
            return protection
    return fake


def test_check_protection_ruleset(monkeypatch):
    monkeypatch.setattr(ss, "gh_api", _api(
        rules=[{"type": "pull_request"}], prot_err=GitHubError("nf", 404)))
    assert ss.check_protection("o/r") == ("main", None, "protected")


def test_check_protection_classic(monkeypatch):
    monkeypatch.setattr(ss, "gh_api", _api(
        rules=[], protection={"required_pull_request_reviews": {}}))
    assert ss.check_protection("o/r") == ("main", None, "protected")


def test_check_protection_unprotected_and_unknown(monkeypatch):
    monkeypatch.setattr(ss, "gh_api", _api(
        rules=[], prot_err=GitHubError("nf", 404)))
    assert "does not require" in ss.check_protection("o/r")[1]
    assert ss.check_protection("o/r")[2] == "unprotected"
    monkeypatch.setattr(ss, "gh_api", _api(
        rules_err=GitHubError("x", 500), prot_err=GitHubError("x", 403)))
    assert "couldn't verify" in ss.check_protection("o/r")[1]
    assert ss.check_protection("o/r")[2] == "unknown"
