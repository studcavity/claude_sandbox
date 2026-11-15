"""Tests for claude_sandbox: CLI parsing, pure helpers, Sandbox lifecycle."""
import argparse
import os
import re
import stat
import subprocess
import types

import pytest

import sandbox_volumes

import claude_sandbox as cs


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_parser_defaults(monkeypatch):
    monkeypatch.delenv("CLAUDE_SANDBOX_MODEL", raising=False)
    args = cs.build_parser().parse_args([])
    assert args.model == cs.DEFAULT_MODEL
    assert args.cli == "incus" and args.max_age == 7
    assert args.image is None  # unset: repo's image: or the default applies
    assert cs.DEFAULT_IMAGE == "images:ubuntu/26.04"
    assert args.github_user == "studcavity"
    assert args.git_email == cs.DEFAULT_GIT_EMAIL
    assert args.repository is None and not args.account_key
    assert os.path.isfile(args.claude_md)


def test_parser_model_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_SANDBOX_MODEL", "opus")
    assert cs.build_parser().parse_args([]).model == "opus"


@pytest.mark.parametrize("argv, msg", [
    (["--repo", "not-a-repo"], "--repo"),
    ([], "a repository is required"),
    (["a/b", "--repo", "c/d"], "once"),
    (["a/b", "--account-key"], "mutually exclusive"),
    (["bad name"], "not a valid repository name"),
    (["x/y/z"], "not a valid owner/name"),
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

    def fake(method, path, body=None, raw=False):
        calls.append((method, path))
        if "/contents/" in path:
            raise cs.GitHubError("nf", 404)
        return {"id": 42}
    monkeypatch.setattr(cs, "gh_api", fake)
    return calls


def _sandbox(make_args, monkeypatch, **kw):
    box = cs.Sandbox(make_args(**kw))
    monkeypatch.setattr(cs.Sandbox, "login", cs.DEFAULT_GITHUB_USER,
                        raising=False)
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
    ({"repo", "workflow"}, True),   # the sandbox could push a live workflow
    ({"repo"}, False),              # the safe default: nothing to say
    (None, True),                   # can't tell: say what's at stake
])
def test_check_workflow_scope_warns_when_workflows_are_pushable(
        monkeypatch, capsys, scopes, warned):
    monkeypatch.setattr(cs, "token_scopes", lambda: scopes)
    cs.check_workflow_scope()
    assert ("warning:" in capsys.readouterr().err) == warned


def test_check_workflow_scope_never_raises(monkeypatch, capsys):
    def down():
        raise cs.GitHubError("GitHub GET / -> 503", 503)
    monkeypatch.setattr(cs, "token_scopes", down)
    cs.check_workflow_scope()
    assert "couldn't verify" in capsys.readouterr().err


def test_launch_runs_self_check_right_after_network(make_args, monkeypatch,
                                                    fake_box):
    box = _sandbox(make_args, monkeypatch)
    order = []
    fake_box.launch = lambda image, ephemeral, config=None, root_size=None: order.append("launch")
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
    def fake(method, path, body=None, raw=False):
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
    src = inspect.getsource(cs._launch)
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


# Every character of the rendered CLAUDE.md is loaded into every session's
# context. The budget is ~6k tokens with room for long per-session notes;
# before raising it, move detail into an on-demand reference file
# (sandbox_ref_deps.md is the model) or cut it.
CLAUDE_MD_BUDGET_CHARS = 23_000
DEPS_REF_BUDGET_CHARS = 8_000


def test_rendered_claude_md_stays_within_the_context_budget(repo_root):
    md = cs.render_claude_md(
        open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read(),
        cs.parse_probe("sudo=yes\ntools=git jq rg fd\nbranch=sbx/quirky-hopper/work"),
        "quirky-hopper", "/home/ubuntu/some-repo", "owner/some-repo", False,
        "- apt: `a`\n- binaries: `b`", session="quirky-hopper",
        protection_note=cs.protection_note("o/r", "main", "protected", ("a", "b")),
        disk_note="- " + "disk note " * 30)
    assert len(md) <= CLAUDE_MD_BUDGET_CHARS, (
        f"{len(md)} chars: move detail to an on-demand reference file")
    ref = cs.render_deps_ref(
        open(os.path.join(repo_root, cs.DEPS_REF_TEMPLATE)).read())
    assert len(ref) <= DEPS_REF_BUDGET_CHARS


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
    src = inspect.getsource(cs._launch)
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
    # the marker is written by the same root exec as the package probe
    (probe,) = [c[1] for c in fake_box.calls if c[0] == "root" and "/etc/claude-sandbox" in c[1]]
    assert "command -v git" in probe
    assert not any(c[0] == "put_file" and c[1] == "/etc/claude-sandbox"
                   for c in fake_box.calls)


def test_template_documents_setup_and_the_hazard_guard(repo_root):
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    guard = '[ -f /etc/claude-sandbox ] || '
    assert guard in t and "Gate hazardous scripts" in t
    flat = " ".join(t.split())
    assert "sudo" in t and "rm -rf" in t and "never weaken or remove" in flat
    assert cs.SANDBOX_MARKER == "/etc/claude-sandbox"
    ref = open(os.path.join(repo_root, cs.DEPS_REF_TEMPLATE)).read()
    assert "\nsetup:" in ref and "idempotent" in ref


# --------------------------------------------------------------------------- #
# defaults, repository resolution and the gh user gate (formerly run.sh)
# --------------------------------------------------------------------------- #
def _norm(argv):
    args = cs.build_parser().parse_args(argv)
    return cs.normalize_args(args)


def test_bare_repository_name_resolves_under_github_user():
    a = _norm(["lightning-bugs"])
    assert a.repo == "studcavity/lightning-bugs"
    assert a.git_name == "studcavity"  # defaults to the github user
    assert _norm(["other/proj", "--github-user", "me"]).repo == "other/proj"
    assert _norm(["proj", "--github-user", "me"]).git_name == "me"
    assert _norm(["proj", "--git-name", "Bob"]).git_name == "Bob"


def test_bare_name_cannot_smuggle_a_path_or_option():
    for bad in ("..", ".", "a b", "x;rm", "a/../b"):
        with pytest.raises(ValueError):
            _norm([bad])


def test_account_wide_key_is_never_implicit():
    with pytest.raises(ValueError, match="repository is required"):
        _norm([])
    assert _norm(["--account-key"]).repo is None
    assert _norm(["--setup-token"]).repo is None  # no GitHub involved


def test_github_user_and_email_defaults_come_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_SANDBOX_GITHUB_USER", "alice")
    monkeypatch.setenv("CLAUDE_SANDBOX_GIT_EMAIL", "a@x")
    a = _norm(["proj"])
    assert (a.repo, a.git_email, a.git_name) == ("alice/proj", "a@x", "alice")


def test_check_expected_user():
    assert cs.check_expected_user("studcavity", "studcavity") is None
    assert cs.check_expected_user("StudCavity", "studcavity") is None
    msg = cs.check_expected_user("mallory", "studcavity")
    assert "mallory" in msg and "studcavity" in msg and "--no-user-check" in msg


@pytest.fixture
def main_env(monkeypatch):
    """main() with every side effect stubbed; records what ran."""
    seen = {"launched": False, "update": 0}
    monkeypatch.setattr(cs.sandbox_update, "check_for_updates",
                        lambda *a, **k: seen.__setitem__("update", seen["update"] + 1))
    monkeypatch.setattr(cs.Sandbox, "login", "studcavity", raising=False)
    monkeypatch.setattr(cs, "check_workflow_scope", lambda: None)
    monkeypatch.setattr(cs, "ensure_base_image", lambda *a, **k: "img")
    monkeypatch.setattr(cs.Sandbox, "load_deps", lambda self: None)
    monkeypatch.setattr(cs.Sandbox, "resolve_usb", lambda self: [])
    monkeypatch.setattr(cs.Sandbox, "launch",
                        lambda self, image: seen.__setitem__("launched", True)
                        or (_ for _ in ()).throw(RuntimeError("stop here")))
    monkeypatch.setattr(cs.Sandbox, "teardown", lambda self: None)
    monkeypatch.setattr(cs.Sandbox, "preflight_github", lambda self: None)
    monkeypatch.setattr(cs.Sandbox, "land", lambda self: None)
    return seen


def test_main_refuses_unexpected_gh_user(main_env, monkeypatch, capsys):
    monkeypatch.setattr(cs.Sandbox, "login", "mallory", raising=False)
    assert cs.main(["proj"]) == 1
    assert "mallory" in capsys.readouterr().err
    assert not main_env["launched"]


def test_main_no_user_check_flag(main_env, monkeypatch):
    monkeypatch.setattr(cs.Sandbox, "login", "mallory", raising=False)
    cs.main(["proj", "--no-user-check"])
    assert main_env["launched"]


def test_main_reports_missing_gh_cleanly(main_env, monkeypatch, capsys):
    def no_gh(self):
        raise cs.GitHubError("the GitHub CLI `gh` is not installed", 0)
    monkeypatch.setattr(cs.Sandbox, "login", property(no_gh), raising=False)
    assert cs.main(["proj"]) == 1
    assert "gh" in capsys.readouterr().err and not main_env["launched"]


def test_setup_token_skips_the_gh_gate(main_env, monkeypatch):
    def boom(self):
        raise AssertionError("must not touch GitHub")
    monkeypatch.setattr(cs.Sandbox, "login", property(boom), raising=False)
    cs.main(["--setup-token"])
    assert main_env["launched"]


def test_update_check_runs_by_default_and_can_be_disabled(main_env):
    cs.main(["proj"])
    assert main_env["update"] == 1
    cs.main(["proj", "--no-update-check"])
    assert main_env["update"] == 1


def test_update_check_happens_before_any_github_or_container_work(
        main_env, monkeypatch):
    order = []
    monkeypatch.setattr(cs.sandbox_update, "check_for_updates",
                        lambda *a, **k: order.append("update"))
    monkeypatch.setattr(cs.Sandbox, "login",
                        property(lambda self: order.append("gh") or "studcavity"),
                        raising=False)
    cs.main(["proj"])
    assert order[:2] == ["update", "gh"]


def test_template_has_no_docker_references(repo_root):
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    assert "docker" not in t.lower() and "podman" not in t.lower()


# --------------------------------------------------------------------------- #
# base image: default Ubuntu 26.04, per-repo `image:` override, --image wins
# --------------------------------------------------------------------------- #
def test_choose_image_precedence():
    assert cs.choose_image(None, None) == ("images:ubuntu/26.04", "default")
    assert cs.choose_image(None, "images:ubuntu/24.04") == (
        "images:ubuntu/24.04", cs.sandbox_deps.DEPS_FILE)
    assert cs.choose_image("images:debian/13", "images:ubuntu/24.04") == (
        "images:debian/13", "--image")  # the host user may pick anything


def test_sandbox_uses_the_repos_image_and_says_so(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="me/p")
    box.deps = {"image": "images:ubuntu/24.04"}
    assert box.choose_image() == "images:ubuntu/24.04"
    assert "overridden" not in box.deps_note


def test_flag_overrides_the_repos_image_and_the_session_is_told(make_args,
                                                                monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="me/p", image="ubuntu:26.04")
    box.deps = {"image": "images:ubuntu/24.04"}
    assert box.choose_image() == "ubuntu:26.04"
    assert "overridden by the launcher flag" in box.deps_note


def test_default_image_without_deps_or_flag(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch)
    assert box.choose_image() == cs.DEFAULT_IMAGE


def test_deps_are_read_before_the_base_image_is_built():
    """The repo's image: must be known before ensure_base_image runs."""
    import inspect
    src = inspect.getsource(cs._launch)
    assert (src.index("box.load_deps()") < src.index("box.choose_image()")
            < src.index("ensure_base_image("))


def test_main_builds_the_base_from_the_repos_image(main_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(cs, "ensure_base_image",
                        lambda cli, source, *a, **k: seen.setdefault("source", source))
    parsed = cs.sandbox_deps.parse("image: images:ubuntu/24.04")
    monkeypatch.setattr(cs.Sandbox, "load_deps",
                        lambda self: setattr(self, "deps", parsed))
    cs.main(["proj"])
    assert seen["source"] == "images:ubuntu/24.04"
    seen.clear()
    cs.main(["proj", "--image", "images:ubuntu/26.04"])
    assert seen["source"] == "images:ubuntu/26.04"


def test_template_documents_image_key(repo_root):
    t = open(os.path.join(repo_root, cs.DEPS_REF_TEMPLATE)).read()
    assert "\nimage: images:ubuntu/24.04" in t and "--image" in t


# --------------------------------------------------------------------------- #
# disk quotas and volumes
# --------------------------------------------------------------------------- #
def test_size_arg():
    assert cs.size_arg("30GiB") == "30GiB"
    assert cs.size_arg("0") is None
    for bad in ("", "-1GiB", "lots", "30 GiB; rm", "0GiB"):
        with pytest.raises(argparse.ArgumentTypeError):
            cs.size_arg(bad)


def test_disk_flags_default_and_disable():
    a = cs.build_parser().parse_args(["r"])
    assert (a.disk_size, a.volume_size) == (cs.DEFAULT_DISK_SIZE,
                                            cs.DEFAULT_VOLUME_SIZE)
    a = cs.build_parser().parse_args(["--disk-size", "0", "--volume-size", "0", "r"])
    assert a.disk_size is None and a.volume_size is None


def test_launch_passes_root_size(make_args, monkeypatch, fake_box):
    box = _sandbox(make_args, monkeypatch, disk_size="30GiB")
    seen = {}
    fake_box.launch = lambda image, ephemeral, config=None, root_size=None: \
        seen.update(root_size=root_size)
    fake_box.wait_network = fake_box.self_check = lambda: None
    fake_box.root = lambda script, **kw: None
    box.box = fake_box
    box.launch("img")
    assert seen == {"root_size": "30GiB"}


def test_repo_workdir():
    assert cs.repo_workdir("me/proj") == "/home/ubuntu/proj"
    assert cs.repo_workdir(None) == "/home/ubuntu"


def test_disk_note_reflects_mounted_volumes(make_args, monkeypatch, fake_box):
    box = _sandbox(make_args, monkeypatch, disk_size="30GiB", volume_size="20GiB")
    assert "Everything shares that one disk" in box.disk_note()
    m = sandbox_volumes.plan("c", "/home/ubuntu/p", "20GiB")
    box.volumes = sandbox_volumes.Volumes("incus", fake_box, m, "20GiB")
    box.volumes.created = [m[0].volume]
    note = box.disk_note()
    assert "30GiB" in note and "`/tmp`" in note and "/home/ubuntu/p" not in note


def test_claude_md_stresses_ephemeral_session(repo_root):
    md = _render(repo_root, [])
    assert "entirely ephemeral" in md and "wip/" in md
    assert "out of memory" in md and "host reboot" in md


def test_claude_md_documents_commit_message_followups(repo_root):
    md = _render(repo_root, [])
    assert "Follow-up:" in md and "git log --grep='Follow-up'" in md


# --------------------------------------------------------------------------- #
# concurrent sessions: identity, branches, landing
# --------------------------------------------------------------------------- #
def test_session_id_is_the_container_name_and_avoids_existing(make_args,
                                                              monkeypatch):
    seen = {}
    monkeypatch.setattr(cs.Container, "list_names",
                        staticmethod(lambda cli: seen.setdefault("n", {"x"})))
    box = cs.Sandbox(make_args(name=None))
    assert box.session_id == box.name and "claude" not in box.name
    assert cs.sandbox_sessions.validate_session(box.name)


def test_bad_name_is_rejected_up_front():
    p = cs.build_parser()
    args = p.parse_args(["repo", "--name", "a/b"])
    with pytest.raises(ValueError):
        cs.normalize_args(args)


def test_land_all_needs_a_repo():
    args = cs.build_parser().parse_args(["--account-key", "--land-all"])
    with pytest.raises(ValueError, match="--land-all"):
        cs.normalize_args(args)


def test_clone_puts_session_on_its_own_branch(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="o/r", name="quirky-hopper")
    cmds = []
    monkeypatch.setattr(box.box, "user", lambda s, **k: cmds.append(s))
    box.clone()
    assert "checkout -q -b sbx/quirky-hopper/work" in cmds[-1]


def test_read_only_session_makes_no_branch(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="o/r", read_only=True)
    cmds = []
    monkeypatch.setattr(box.box, "user", lambda s, **k: cmds.append(s))
    box.clone()
    assert len(cmds) == 1 and "checkout" not in cmds[0]


def test_deploy_key_title_names_host_and_session(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="o/r", name="quirky-hopper")
    posted = []
    monkeypatch.setattr(box.box, "user", lambda s, **k: type(
        "R", (), {"stdout": "ssh-ed25519 AAA"})())
    monkeypatch.setattr(cs, "gh_api", lambda m, p, body=None, raw=False:
                        posted.append(body) or {"id": 7, "ssh_keys": []})
    box.provision_key()
    key = [b for b in posted if b and "key" in b][0]
    assert key["title"] == f"ephemeral {cs.sandbox_sessions.host_id()}:quirky-hopper"


@pytest.mark.parametrize("kw", [dict(no_pr=True), dict(read_only=True),
                                dict(repo=None)])
def test_land_is_skipped_when_it_should_be(make_args, monkeypatch, kw):
    base = dict(repo="o/r", no_pr=False)
    box = _sandbox(make_args, monkeypatch, **{**base, **kw})
    box.key_id = 1
    monkeypatch.setattr(cs.sandbox_land, "land", lambda *a: pytest.fail("landed"))
    box.land()


def test_land_is_skipped_before_a_key_exists(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="o/r", no_pr=False)
    monkeypatch.setattr(cs.sandbox_land, "land", lambda *a: pytest.fail("landed"))
    box.land()  # key_id is None: the session never got that far


def test_land_failure_never_blocks_teardown(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="o/r", no_pr=False)
    box.key_id = 1

    def boom(*a):
        raise cs.GitHubError("down", 0)
    monkeypatch.setattr(cs.sandbox_land, "land", boom)
    box.land()  # logs a warning, does not raise


def test_land_passes_session_and_confirm_only(make_args, monkeypatch):
    """Setup-script holds are decided inside sandbox_land (from the default
    branch's deps file), not by what this launch happened to parse."""
    box = _sandbox(make_args, monkeypatch, repo="o/r", no_pr=False,
                   name="quirky-hopper", no_deps=True)
    box.key_id, box.deps = 1, None
    got = []
    monkeypatch.setattr(cs.sandbox_land, "land", lambda *a: got.append(a) or [])
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    box.land()
    assert got == [("o/r", "quirky-hopper", None)]  # no prompt
    got.clear()
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: True)
    box.land()
    assert got == [("o/r", "quirky-hopper", cs.confirm_held)]


def test_template_tells_session_its_identity_and_branch_rules(repo_root):
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    md = cs.render_claude_md(
        t, {"sudo": True, "tools": [], "branch": "sbx/quirky-hopper/work",
            "usb": set()}, "quirky-hopper", "/h/r", "o/r", False, "",
        session="quirky-hopper")
    assert "session\n`quirky-hopper`" in md.replace("session `", "session\n`")
    assert "sbx/quirky-hopper/" in md and "Never push to the default" in md
    assert "force-with-lease" in md and "claude/" not in md.replace(
        "claude_sandbox", "").replace("claude-sandbox", "").replace(
        "~/.claude/", "")


def test_deps_reference_is_written_and_the_prompt_points_at_it(
        make_args, monkeypatch, repo_root):
    """The deps schema is read on demand: it must land where CLAUDE.md says,
    fully rendered, and CLAUDE.md must name that path."""
    writes = {}

    class Box:
        def user(self, script, input=None, **kw):
            writes[script] = input
            return types.SimpleNamespace(stdout="sudo=yes\ntools=git\nbranch=")

    box = _sandbox(make_args, monkeypatch, repo="me/p")
    box.box = Box()
    box.write_claude_md("/home/ubuntu/p")
    (ref,) = [v for k, v in writes.items() if k == f"cat > {cs.DEPS_REF_PATH}"]
    assert cs.sandbox_deps.DEPS_FILE in ref and "last_generated" in ref
    assert not re.search(r"\$[a-z_{]", ref), "unsubstituted placeholder"
    (main,) = [v for k, v in writes.items() if "CLAUDE.md" in k]
    assert cs.DEPS_REF_PATH in main


def test_ruleset_file_requires_pull_requests_and_blocks_rewrites(repo_root):
    import json
    rs = json.load(open(os.path.join(repo_root, "rulesets",
                                     "protect-main.json")))
    types = {r["type"] for r in rs["rules"]}
    assert {"pull_request", "non_fast_forward", "deletion"} <= types
    assert rs["bypass_actors"] == [] and rs["enforcement"] == "active"
    assert rs["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]


def test_actions_run_after_prs_even_with_no_pr_and_honour_no_actions(
        make_args, monkeypatch):
    got = []
    monkeypatch.setattr(cs.sandbox_actions, "run",
                        lambda *a, **kw: got.append((a, kw)) or [])
    monkeypatch.setattr(cs.sandbox_land, "land",
                        lambda *a: pytest.fail("landed"))
    box = _sandbox(make_args, monkeypatch, repo="o/r", no_pr=True,
                   name="quirky-hopper")
    box.key_id = 1
    box.land()
    # the caps travel from the flags to the filer
    assert got == [(("o/r", "quirky-hopper"),
                    {"limits": {"actions": 25, "issues": 10}})]
    got.clear()
    box.args.no_actions = True
    box.land()
    assert got == []


def test_action_failure_never_blocks_teardown(make_args, monkeypatch):
    def boom(*a):
        raise cs.GitHubError("down", 0)
    monkeypatch.setattr(cs.sandbox_actions, "run", boom)
    monkeypatch.setattr(cs.sandbox_land, "land", lambda *a: [])
    box = _sandbox(make_args, monkeypatch, repo="o/r")
    box.key_id = 1
    box.land()


def test_template_asks_for_a_todo_file_per_issue_action(repo_root):
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    assert "`todo/<id>.md`" in t and "same commit" in t
    assert "fresh agent" in t


def test_template_says_the_todo_brief_describes_but_does_not_dictate(repo_root):
    t = " ".join(open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read().split())
    assert "describe a problem and some potential solutions" in t
    assert "NOT a work order" in t and "must not dictate" in t


def test_template_names_credits_as_a_way_work_is_lost(repo_root):
    """Running out of credits ends a session like a crash; the sandbox CLAUDE.md
    must say so inside the ephemerality section, where the push habits are."""
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()
    start = t.index("## This session is entirely ephemeral")
    section = t[start:t.index("\n## ", start + 1)]
    assert "credits" in section.lower()
    assert "no warning" in section and "commit and push after every coherent" in section


# --- the checker inside the sandbox (plan 4d) -------------------------------------- #
def test_session_tool_modules_cover_the_checkers_whole_import_graph(repo_root):
    """If sandbox_check (or anything it imports) gains a MODULE-LEVEL import, the
    copy in the sandbox must too, or `sandbox-check` dies there with ImportError.
    Function-level imports (sandbox_github in --post-status, sandbox_land in
    sandbox_actions.run) are the GitHub paths a session never takes there."""
    import ast
    seen, todo = set(), ["sandbox_check"]
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        tree = ast.parse(open(os.path.join(repo_root, mod + ".py")).read())
        for node in ast.iter_child_nodes(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            todo += [n for n in names if os.path.exists(os.path.join(repo_root, n + ".py"))]
    assert seen == set(cs.SESSION_TOOL_MODULES)


def test_session_tool_files_are_exactly_the_modules_plus_an_executable_wrapper(repo_root):
    files = cs.session_tool_files(repo_root)
    paths = [p for p, _, _ in files]
    assert paths == ([f"{cs.SESSION_TOOL_DIR}/{m}.py" for m in cs.SESSION_TOOL_MODULES]
                     + [cs.SESSION_TOOL_BIN])
    modes = {p: m for p, _, m in files}
    assert modes[cs.SESSION_TOOL_BIN] == "0755" and modes[paths[0]] == "0644"
    assert files[0][1].startswith(b'"""sandbox_check.py')


class ArgvBox:
    """FakeBox that also records the stdin given to root_argv."""
    def __init__(self):
        self.calls = []

    def user(self, script, **kw):
        self.calls.append(("user", script, kw.get("input")))
        return type("R", (), {"stdout": "ssh-ed25519 AAAA\n"})()

    def root_argv(self, argv, env=None, cwd=None, input=None, capture=True):
        self.calls.append(("root_argv", list(argv), input))

    def put_file(self, *a, **k):
        self.calls.append(("put_file", *a))


def test_install_session_tools_is_two_execs_and_one_tar_stream(make_args, monkeypatch, repo_root):
    import io
    import tarfile
    box = _sandbox(make_args, monkeypatch, repo="o/r")
    box.box = ArgvBox()
    box.install_session_tools()
    assert [c[0] for c in box.box.calls] == ["user", "root_argv"]        # not 9
    assert "mkdir -p" in box.box.calls[0][1] and cs.SESSION_TOOL_DIR in box.box.calls[0][1]
    argv, data = box.box.calls[1][1], box.box.calls[1][2]
    assert argv == ["tar", "-xzf", "-", "-C", "/"]
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert set(members) == {p.lstrip("/") for p, _, _ in cs.session_tool_files(repo_root)}
        wrapper = members[cs.SESSION_TOOL_BIN.lstrip("/")]
        assert wrapper.mode == 0o755 and members[
            f"{cs.SESSION_TOOL_DIR.lstrip('/')}/sandbox_check.py"].mode == 0o644
        assert all(m.uid == 0 and m.gid == 0 and m.mtime == 0 for m in members.values())
        assert tar.extractfile(wrapper).read() == cs.SESSION_TOOL_WRAPPER.encode()


def test_the_archive_is_deterministic_and_cannot_escape_the_root(repo_root):
    files = cs.session_tool_files(repo_root)
    assert cs.session_tool_archive(files) == cs.session_tool_archive(files)
    import io
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(cs.session_tool_archive(files))) as tar:
        for m in tar.getmembers():
            assert not m.name.startswith("/") and ".." not in m.name.split("/")
            assert m.isreg()


@pytest.mark.parametrize("kw", [dict(repo=None), dict(repo="o/r", read_only=True)])
def test_no_tools_without_a_repo_to_commit_to(make_args, monkeypatch, fake_box, kw):
    box = _sandbox(make_args, monkeypatch, **kw)
    box.box = fake_box
    box.install_session_tools()
    assert fake_box.calls == []


def test_the_installed_wrapper_really_lints_a_clones_commits(repo_root, tmp_path):
    """Run the very files we install, the way the session would: a clone with an
    `origin`, a session branch, no arguments."""
    import subprocess

    def sh(*a, cwd=None, env=None, check=True):
        return subprocess.run(a, cwd=cwd, env=env, check=check, text=True,
                              capture_output=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    origin, clone, home = tmp_path / "origin", tmp_path / "clone", tmp_path / "home"
    sh(*git, "init", "-q", "-b", "main", str(origin))
    (origin / "a.txt").write_text("1")
    sh(*git, "-C", str(origin), "add", "-A")
    sh(*git, "-C", str(origin), "commit", "-q", "-m", "feat: base")
    sh(*git, "clone", "-q", str(origin), str(clone))
    for path, data, _ in cs.session_tool_files(repo_root):
        dest = home / os.path.relpath(path, cs.HOME)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    wrapper = home / os.path.relpath(cs.SESSION_TOOL_BIN, cs.HOME)
    env = {**os.environ, "HOME": str(home)}

    sh(*git, "-C", str(clone), "switch", "-q", "-c", "sbx/quirky-hopper/work")
    (clone / "b.txt").write_text("2")
    sh(*git, "-C", str(clone), "add", "-A")
    sh(*git, "-C", str(clone), "commit", "-q", "-m",
       "feat: ok")
    ok = sh("sh", str(wrapper), cwd=clone, env=env, check=False)
    assert ok.returncode == 0 and "0 error(s)" in ok.stdout, ok.stdout + ok.stderr

    (clone / "c.txt").write_text("3")
    sh(*git, "-C", str(clone), "add", "-A")
    sh(*git, "-C", str(clone), "commit", "-q", "-m", "fixup! feat: ok")   # no trailer either
    bad = sh("sh", str(wrapper), cwd=clone, env=env, check=False)
    assert bad.returncode == 1 and "::error::" in bad.stdout and "fixup" in bad.stdout
    # arguments pass straight through to the checker
    assert "usage" in sh("sh", str(wrapper), "--help", cwd=clone, env=env).stdout


# --- batched container scripts (plan 1d), executed for real ------------------------ #
def run_sh(script, env=None, cwd=None):
    import subprocess
    return subprocess.run(["sh", "-c", script], env={**os.environ, **(env or {})},
                          cwd=cwd, capture_output=True, text=True)


@pytest.mark.parametrize("name", ["quirky-hopper", "has space", "q'uote", "$(touch pwned)", "a;b"])
def test_marker_script_writes_the_name_verbatim_and_mode_0644(tmp_path, name):
    path = tmp_path / "marker"
    r = run_sh(cs.marker_script(name, str(path)), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert path.read_text() == name + "\n"
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert not (tmp_path / "pwned").exists()           # the name was quoted, not run


def test_git_config_script_sets_everything_in_a_clean_home(tmp_path):
    script = cs.git_config_script("Ada L", "ada@example.com")
    r = run_sh(script, env={"HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"})
    assert r.returncode == 0, r.stderr
    out = run_sh("git config --global --list", env={"HOME": str(tmp_path)}).stdout
    for line in ("user.name=Ada L", "user.email=ada@example.com", "pull.rebase=true",
                 "rebase.autostash=true", "merge.ff=only", "push.autosetupremote=true",
                 "url.git@github.com:.insteadof=https://github.com/"):
        assert line in out, out


def test_git_config_script_without_identity_and_with_hostile_values(tmp_path):
    assert "user.name" not in cs.git_config_script(None, None)
    r = run_sh(cs.git_config_script("x'; touch pwned; '", "$(touch pwned2)"),
               env={"HOME": str(tmp_path)}, cwd=tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()
    out = run_sh("git config --global user.name", env={"HOME": str(tmp_path)}).stdout
    assert out.strip() == "x'; touch pwned; '"


def test_provision_key_is_two_execs_and_keeps_its_order(make_args, monkeypatch):
    box = _sandbox(make_args, monkeypatch, repo="o/r", name="quirky-hopper",
                   git_name="Ada", git_email="a@x.org")
    box.box = ArgvBox()
    api = []
    monkeypatch.setattr(cs, "gh_api", lambda m, p, body=None, raw=False:
                        api.append((m, p)) or ({"ssh_keys": ["ssh-ed25519 KEY"]} if p == "/meta"
                                               else {"id": 7}))
    box.provision_key()
    execs = box.box.calls
    assert [c[0] for c in execs] == ["user", "user"]                   # was up to 7
    assert "ssh-keygen" in execs[0][1] and execs[0][1].rstrip().endswith("id_ed25519.pub")
    assert execs[1][2] == "github.com ssh-ed25519 KEY\n"                # known_hosts via stdin
    assert "git config --global user.name Ada" in execs[1][1]
    assert "commit.gpgsign true" in execs[1][1]                         # signing key, same exec
    assert api == [("GET", "/meta"), ("POST", "/repos/o/r/keys")]      # key still added after meta
    assert box.key_id == 7


def test_the_real_tar_binary_extracts_the_archive_to_the_right_places(repo_root, tmp_path):
    """What the container runs (`tar -xzf - -C /`), against a temp root."""
    import subprocess
    files = cs.session_tool_files(repo_root)
    root = tmp_path / "root"
    for d in (cs.SESSION_TOOL_DIR, os.path.dirname(cs.SESSION_TOOL_BIN)):
        (root / d.lstrip("/")).mkdir(parents=True)             # made by `mkdir -p` as the user
    r = subprocess.run(["tar", "-xzf", "-", "-C", str(root)], input=cs.session_tool_archive(files),
                       capture_output=True)
    assert r.returncode == 0, r.stderr
    for path, data, mode in files:
        f = root / path.lstrip("/")
        assert f.read_bytes() == data
        assert oct(f.stat().st_mode & 0o777) == oct(int(mode, 8))


@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("YES", True), ("", False), ("n", False), ("maybe", False)])
def test_confirm_held_defaults_to_no(monkeypatch, capsys, answer, expected):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert cs.confirm_held("sbx/s/\x1b[31mw", ".github/x") is expected
    assert "\x1b" not in capsys.readouterr().err


def test_confirm_held_treats_eof_as_no(monkeypatch):
    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert cs.confirm_held("sbx/s/w", ".github/x") is False
