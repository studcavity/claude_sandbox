import json
import subprocess
import sys

import pytest

from claude_sandbox import claude_config as cc
def test_trust_only_for_own_repo_case_insensitive():
    assert cc.trust_decision("StudCavity", "studcavity") == (True, None)


@pytest.mark.parametrize("owner,login", [("someone-else", "studcavity"),
                                         ("an-org", "studcavity"),
                                         ("", "studcavity"), (None, "x"),
                                         ("studcavity", "")])
def test_no_trust_for_foreign_or_unknown_owner(owner, login):
    trusted, note = cc.trust_decision(owner, login)
    assert trusted is False and note


def test_state_with_credentials_and_trust():
    st = cc.claude_state("/home/ubuntu/r", {"CLAUDE_CODE_OAUTH_TOKEN": "t"},
                         True)
    assert st[".claude.json"] == {
        "hasCompletedOnboarding": True,
        "projects": {"/home/ubuntu/r": {"hasTrustDialogAccepted": True}}}
    assert st[".claude/settings.json"] == {"theme": "dark-daltonized"}


def test_state_without_credentials_keeps_login_flow_and_no_trust():
    st = cc.claude_state("/w", {}, False)
    assert st[".claude.json"] == {}  # login flow must still appear
    assert st[".claude/settings.json"]["theme"] == cc.THEME


def test_api_key_approved_by_last_20_chars_only():
    key = "sk-ant-api03-" + "x" * 40 + "0123456789ABCDEFGHIJ"
    st = cc.claude_state("/w", {"ANTHROPIC_API_KEY": key}, False)
    resp = st[".claude.json"]["customApiKeyResponses"]
    assert resp == {"approved": ["0123456789ABCDEFGHIJ"], "rejected": []}
    assert key not in json.dumps(st)  # never write the whole key to disk


def run_merge(home, state):
    return subprocess.run([sys.executable, "-I", "-c", cc.MERGE_SCRIPT],
                          input=cc.merge_input(state), text=True,
                          capture_output=True, env={"HOME": str(home)},
                          check=True)


def test_merge_script_creates_files_and_preserves_existing_keys(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({
        "userID": "keep", "projects": {"/other": {"hasTrustDialogAccepted": False,
                                                    "x": 1}}}))
    (tmp_path / ".claude/settings.json").write_text('{"model": "m"}')
    run_merge(tmp_path, cc.claude_state("/w", {"CLAUDE_CODE_OAUTH_TOKEN": "t"},
                                        True))
    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert cfg["userID"] == "keep" and cfg["hasCompletedOnboarding"] is True
    assert cfg["projects"]["/other"] == {"hasTrustDialogAccepted": False, "x": 1}
    assert cfg["projects"]["/w"] == {"hasTrustDialogAccepted": True}
    assert json.loads((tmp_path / ".claude/settings.json").read_text()) == {
        "model": "m", "theme": "dark-daltonized"}
    assert not list(tmp_path.rglob("*.tmp"))


def test_merge_script_starts_from_nothing(tmp_path):
    run_merge(tmp_path, cc.claude_state("/w", {}, False))
    assert json.loads((tmp_path / ".claude/settings.json").read_text()) == {
        "theme": "dark-daltonized"}
