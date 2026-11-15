"""Tests for claude_sandbox: CLI parsing, pure helpers, Sandbox lifecycle."""
import os
import re
import stat
import subprocess

import pytest

import claude_sandbox as cs


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_parser_defaults(monkeypatch):
    monkeypatch.delenv("CLAUDE_SANDBOX_MODEL", raising=False)
    args = cs.build_parser().parse_args([])
    assert args.model == cs.DEFAULT_MODEL
    assert args.cli == "lxc" and args.max_age == 7
    assert os.path.isfile(args.claude_md)


def test_parser_model_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_SANDBOX_MODEL", "opus")
    assert cs.build_parser().parse_args([]).model == "opus"


@pytest.mark.parametrize("argv, msg", [
    (["--repo", "not-a-repo"], "--repo"),
    (["--claude-md", "/nonexistent/x.md"], "--claude-md"),
])
def test_main_rejects_bad_args(argv, msg, capsys):
    with pytest.raises(SystemExit) as e:
        cs.main(argv)
    assert e.value.code == 2
    assert msg in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_token_file_roundtrip(tmp_path):
    path = str(tmp_path / "sub" / "oauth_token")
    assert cs.read_token_file(path) is None
    cs.save_token_file(path, "sk-ant-xyz")
    assert cs.read_token_file(path) == "sk-ant-xyz"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_claude_secrets_precedence(tmp_path):
    tf = tmp_path / "t"
    tf.write_text("from-file\n")
    assert cs.claude_secrets({}, str(tf)) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "from-file"}
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "from-env", "ANTHROPIC_API_KEY": "k"}
    assert cs.claude_secrets(env, str(tf)) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "from-env", "ANTHROPIC_API_KEY": "k"}
    assert cs.claude_secrets({}, str(tmp_path / "missing")) == {}


def test_secrets_script_quotes_values():
    script = cs.secrets_script({"K": "a'b $x"})
    out = subprocess.run(["bash", "-c", script + 'printf %s "$K"'],
                         capture_output=True, text=True, check=True).stdout
    assert out == "a'b $x"


@pytest.mark.parametrize("kw, expect", [
    ({}, "exec claude"),
    ({"model": "opus"}, "exec claude --model opus"),
    ({"model": "Default"}, "exec claude"),
    ({"shell": True, "model": "opus"}, "exec bash -l"),
    ({"tmux": True, "model": "opus"},
     "exec tmux new-session -s claude 'claude --model opus'"),
])
def test_session_script(kw, expect):
    assert cs.session_script("/w", "", **kw) == f"cd /w && {expect}"


def test_session_script_quotes_workdir_and_prefixes_secrets():
    s = cs.session_script("/home/u/my repo", ". f && ")
    assert s.startswith(". f && cd '/home/u/my repo' && ")


def test_parse_probe():
    facts = cs.parse_probe("sudo=yes\ntools=git rg \nbranch=main\njunk\n")
    assert facts == {"sudo": True, "tools": ["git", "rg"], "branch": "main"}
    assert cs.parse_probe("") == {"sudo": False, "tools": [], "branch": ""}


@pytest.mark.parametrize("repo, read_only, needle", [
    ("me/p", False, "write access"),
    ("me/p", True, "read-only"),
    (None, False, "No repo was cloned"),
])
def test_render_claude_md_real_template(repo_root, repo, read_only, needle):
    with open(os.path.join(repo_root, "sandbox_CLAUDE.md")) as f:
        template = f.read()
    md = cs.render_claude_md(
        template, cs.parse_probe("sudo=no\ntools=git jq\nbranch=main"),
        container="claude-1", workdir="/home/ubuntu/p", repo=repo,
        read_only=read_only, deps_note="DEPS-NOTE")
    assert needle in md
    assert "`claude-1`" in md and "DEPS-NOTE" in md and "`git`, `jq`" in md
    assert "No passwordless `sudo`" in md
    # every placeholder was substituted
    assert not re.search(r"\$[a-z_{]", md), "unsubstituted placeholder"


# --------------------------------------------------------------------------- #
# Sandbox lifecycle (no real containers / network)
# --------------------------------------------------------------------------- #
@pytest.fixture
def gh(monkeypatch):
    calls = []

    def fake(method, path, token, body=None, raw=False):
        calls.append((method, path))
        if path.endswith("/contents/" + cs.sandbox_deps.DEPS_FILE):
            raise cs.GitHubError("nf", 404)
        return {"id": 42}
    monkeypatch.setattr(cs, "gh_api", fake)
    return calls


def _sandbox(make_args, monkeypatch, **kw):
    box = cs.Sandbox(make_args(**kw))
    monkeypatch.setattr(cs.Sandbox, "token", "TOKEN", raising=False)
    return box


def test_load_deps_missing_file_is_noted(make_args, monkeypatch, gh):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    assert box.load_deps() is None
    assert "has no" in box.deps_note


def test_load_deps_no_deps_flag_skips_fetch(make_args, monkeypatch, gh):
    box = _sandbox(make_args, monkeypatch, repo="me/p", no_deps=True)
    assert box.load_deps() is None and gh == []
    assert "--no-deps" in box.deps_note


def test_teardown_deletes_deploy_key_and_container(make_args, monkeypatch, gh):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    deleted = []
    monkeypatch.setattr(box.box, "delete", lambda: deleted.append(True))
    box.key_id, box.container_up = 42, True
    box.teardown()
    box.teardown()  # idempotent
    assert gh == [("DELETE", "/repos/me/p/keys/42")]
    assert deleted == [True]


def test_teardown_still_destroys_container_if_key_delete_fails(
        make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch)

    def boom(*a, **k):
        raise cs.GitHubError("down", 500)
    monkeypatch.setattr(cs, "gh_api", boom)
    deleted = []
    monkeypatch.setattr(box.box, "delete", lambda: deleted.append(True))
    box.key_id, box.container_up = 7, True
    box.teardown()
    assert deleted == [True]


@pytest.mark.parametrize("scopes, warned", [
    ({"repo", "workflow"}, False),
    ({"repo"}, True),
    (None, True),
])
def test_check_workflow_scope_warns_only_on_failure(monkeypatch, capsys,
                                                    scopes, warned):
    monkeypatch.setattr(cs, "token_scopes", lambda t: scopes)
    cs.check_workflow_scope("t")
    assert ("warning:" in capsys.readouterr().err) == warned


def test_check_workflow_scope_never_raises(monkeypatch, capsys):
    def down(t):
        raise cs.GitHubError("GitHub GET / -> 503", 503)
    monkeypatch.setattr(cs, "token_scopes", down)
    cs.check_workflow_scope("t")
    assert "couldn't verify" in capsys.readouterr().err


def test_launch_runs_self_check_right_after_network(make_args, monkeypatch,
                                                    fake_box):
    box = _sandbox(make_args, monkeypatch)
    order = []
    fake_box.launch = lambda image, ephemeral, config=None: order.append("launch")
    fake_box.wait_network = lambda: order.append("network")
    fake_box.self_check = lambda: order.append("self_check")
    fake_box.root = lambda script, **kw: order.append("root")
    box.box = fake_box
    box.launch("ubuntu:24.04")
    assert order[:3] == ["launch", "network", "self_check"]


def test_launch_config_only_sets_nesting_when_asked(make_args):
    assert cs.launch_config(make_args()) == {}
    assert cs.launch_config(make_args(nesting=True)) == {
        "security.nesting": "true"}


def test_setup_nesting_is_noop_without_flag(make_args, monkeypatch, fake_box):
    box = _sandbox(make_args, monkeypatch)
    box.box = fake_box
    box.setup_nesting()
    assert fake_box.calls == []
    box = _sandbox(make_args, monkeypatch, nesting=True)
    box.box = fake_box
    box.setup_nesting()
    assert fake_box.calls == [("root", cs.NESTING_SETUP)]


def test_nesting_setup_script_shape():
    s = cs.NESTING_SETUP
    assert s.index("command -v incus") < s.index("subuid")  # guarded first
    assert "root:100000:65536" in s and "incus admin init --minimal" in s
    assert "SocketMode=0666" in s


def test_parser_has_nesting_flag():
    assert cs.build_parser().parse_args(["--nesting"]).nesting is True
