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
    assert facts == {"sudo": True, "tools": ["git", "rg"], "branch": "main",
                     "usb": set()}
    assert cs.parse_probe("") == {"sudo": False, "tools": [], "branch": "",
                                  "usb": set()}


def test_parse_probe_usb_keeps_only_well_formed_ids():
    facts = cs.parse_probe("usb=2E8A:000c junk 1234:abcd; $(x) 2e8a:\n")
    assert facts["usb"] == {("2e8a", "000c")}


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
        if "/contents/" in path:
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


# --------------------------------------------------------------------------- #
# USB passthrough: approval flow, ordering, CLAUDE.md
# --------------------------------------------------------------------------- #
HOST_YAML = (b'usb:\n  - name: probe\n    vendor_id: "2e8a"\n'
             b'    product_id: "000c"\n    description: Debug probe\n')


@pytest.fixture
def gh_host(monkeypatch):
    """GitHub serving a host file that requests 2e8a:000c."""
    def fake(method, path, token, body=None, raw=False):
        if path.endswith("/" + cs.sandbox_host.HOST_FILE):
            return HOST_YAML
        raise cs.GitHubError("nf", 404)
    monkeypatch.setattr(cs, "gh_api", fake)


def test_usb_request_alone_attaches_nothing(make_args, monkeypatch, gh_host,
                                            fake_box):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    box.box = fake_box
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    (dev,) = box.resolve_usb()
    assert dev["approved"] is False
    box.attach_usb()
    assert not [c for c in fake_box.calls if c[0] == "add_usb"]


def test_usb_prompt_yes_attaches(make_args, monkeypatch, gh_host, fake_box):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    box.box = fake_box
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    box.resolve_usb()
    box.attach_usb()
    assert ("add_usb", "2e8a", "000c") in fake_box.calls


def test_usb_prompt_default_is_no(make_args, monkeypatch, gh_host):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert box.resolve_usb()[0]["approved"] is False


def test_usb_flag_grants_without_repo(make_args, monkeypatch, fake_box):
    box = _sandbox(make_args, monkeypatch, usb=["1234:abcd"])
    box.box = fake_box
    box.resolve_usb()
    box.attach_usb()
    assert ("add_usb", "1234", "abcd") in fake_box.calls


def test_no_usb_overrides_everything(make_args, monkeypatch, gh_host,
                                     fake_box):
    box = _sandbox(make_args, monkeypatch, repo="me/p", usb=["2e8a:000c"],
                   no_usb=True)
    box.box = fake_box
    assert box.resolve_usb() == []
    box.attach_usb()
    assert fake_box.calls == []


def test_usb_allowlist_grants_without_prompt(make_args, monkeypatch, gh_host,
                                             tmp_path):
    allow = tmp_path / "allow"
    allow.write_text("2e8a:000c me/p\n")
    box = _sandbox(make_args, monkeypatch, repo="me/p", usb_allow=str(allow))
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    assert box.resolve_usb()[0]["approved"] is True


def test_usb_bad_allowlist_aborts_launch(make_args, monkeypatch, gh_host,
                                         tmp_path):
    allow = tmp_path / "allow"
    allow.write_text("2e8a\n")
    box = _sandbox(make_args, monkeypatch, repo="me/p", usb_allow=str(allow))
    with pytest.raises(RuntimeError, match="USB allowlist"):
        box.resolve_usb()


def test_usb_attached_after_deps_before_key(monkeypatch):
    """Source-order guard: installers run before hardware is present and
    hardware is attached before any credential is provisioned."""
    import inspect
    src = inspect.getsource(cs.main)
    assert (src.index("box.install_deps()") < src.index("box.attach_usb()")
            < src.index("box.provision_key()"))


@pytest.mark.parametrize("bad", ["2e8a", "2e8a:", ":000c", "2e8a:*",
                                 "xyz1:0000", "2e8a:000c;rm", "2e8a:00000"])
def test_usb_flag_rejects_malformed(bad, capsys):
    with pytest.raises(SystemExit):
        cs.build_parser().parse_args(["--usb", bad])


def test_usb_flag_normalises_case():
    assert cs.build_parser().parse_args(["--usb", "2E8A:000C"]).usb == ["2e8a:000c"]


def _render(repo_root, devices, visible=""):
    with open(os.path.join(repo_root, "sandbox_CLAUDE.md")) as f:
        template = f.read()
    return cs.render_claude_md(
        template, cs.parse_probe(f"sudo=no\nusb={visible}"), container="c1",
        workdir="/w", repo="me/p", read_only=False, deps_note="D",
        usb_devices=devices, cli="incus")


def test_claude_md_usb_states(repo_root):
    dev = lambda n, p, ok: dict(name=n, vendor_id="2e8a", product_id=p,
                                description="Probe", tools=["pyocd"],
                                source="repo", approved=ok)
    md = _render(repo_root, [dev("seen", "000c", True), dev("gone", "0003", True),
                             dev("no", "0004", False)], "2e8a:000c")
    assert "`seen` 2e8a:000c, Probe (tools: `pyocd`): attached and visible" in md
    assert "`gone` 2e8a:0003, Probe (tools: `pyocd`): attached but not visible" in md
    assert "not approved** by the host user" in md
    assert ("incus config device add c1 usb-2e8a-000c usb vendorid=2e8a "
            "productid=000c uid=1000 gid=1000 mode=0660") in md
    assert not re.search(r"\$[a-z_{]", md), "unsubstituted placeholder"


def test_claude_md_without_usb_says_so(repo_root):
    md = _render(repo_root, [])
    assert "No USB devices were requested." in md
    assert "config device add c1" not in md


# --------------------------------------------------------------------------- #
# setup: scripts run in the container
# --------------------------------------------------------------------------- #
SETUP = {"name": "tc", "path": "tools/setup.sh", "args": ["--x", "a b"],
         "env": {"K": "v"}, "timeout": 99}


def test_setup_argv_has_no_shell_and_is_bounded():
    assert cs.setup_argv(SETUP) == [
        "timeout", "--kill-after=10", "99", "bash", "-l", "./tools/setup.sh",
        "--x", "a b"]


def test_run_setup_runs_in_clone_without_stdin_and_reports(make_args,
                                                           monkeypatch, fake_box):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    seen = []

    def user_argv(argv, env=None, cwd=None, **kw):
        seen.append((argv, env, cwd, kw))
        return subprocess.CompletedProcess(argv, 0)
    fake_box.user_argv = user_argv
    box.box, box.deps = fake_box, {"setup": [SETUP]}
    box.run_setup("/home/ubuntu/p")
    argv, env, cwd, kw = seen[0]
    assert cwd == "/home/ubuntu/p" and env == {"K": "v"}
    assert kw == {"check": False, "input": ""}
    assert "`tools/setup.sh`: succeeded" in box.deps_note


@pytest.mark.parametrize("rc, word", [(1, "status 1"), (124, "timed out")])
def test_run_setup_failure_is_reported_not_raised(make_args, monkeypatch,
                                                  fake_box, rc, word):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    fake_box.user_argv = lambda argv, **kw: subprocess.CompletedProcess(argv, rc)
    box.box, box.deps = fake_box, {"setup": [SETUP]}
    box.run_setup("/w")
    assert "FAILED" in box.deps_note and word in box.deps_note


def test_run_setup_noop_without_setup(make_args, monkeypatch, fake_box):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    fake_box.user_argv = lambda *a, **k: pytest.fail("must not run")
    box.box = fake_box
    box.run_setup("/w")  # deps None
    box.deps = {"setup": []}
    box.run_setup("/w")


def test_setup_runs_after_clone_and_before_credentials():
    import inspect
    src = inspect.getsource(cs.main)
    assert (src.index("box.clone()") < src.index("box.run_setup(")
            < src.index("box.session("))
    # credentials are only pushed by session(); setup must not touch them
    assert "push_secrets" not in inspect.getsource(cs.Sandbox.run_setup)


def test_launch_writes_sandbox_marker(make_args, monkeypatch, fake_box):
    box = _sandbox(make_args, monkeypatch)
    for m in ("launch", "wait_network"):
        setattr(fake_box, m, lambda *a, **k: None)
    box.box = fake_box
    box.launch("ubuntu:24.04")
    assert any(c[0] == "put_file" and c[1] == "/etc/claude-sandbox"
               for c in fake_box.calls)


def test_template_documents_setup_and_the_hazard_guard(repo_root):
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    guard = '[ -f /etc/claude-sandbox ] || '
    assert guard in t and "Gate hazardous scripts" in t
    assert "sudo" in t and "rm -rf" in t and "never weaken or remove" in t
    assert "\nsetup:" in t and cs.SANDBOX_MARKER == "/etc/claude-sandbox"
