"""Tests for sandbox_containers.Container: the CLI commands it builds."""
import subprocess

import pytest

import sandbox_containers as sc


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    return seen


def test_launch_and_delete(calls):
    c = sc.Container("incus", "box")
    c.launch("img", ephemeral=True)
    c.delete()
    assert calls[0][0] == ["incus", "launch", "img", "box", "--ephemeral"]
    assert calls[1][0] == ["incus", "delete", "--force", "box"]
    assert calls[1][1]["check"] is False


def test_root_argv_passes_env_and_cwd_without_shell(calls):
    sc.Container("lxc", "box").root_argv(["echo", "a b"], env={"K": "v"},
                                         cwd="/tmp")
    assert calls[0][0] == ["lxc", "exec", "box", "--env", "K=v",
                           "--cwd", "/tmp", "--", "echo", "a b"]


def test_put_file_streams_via_stdin(calls):
    sc.Container("lxc", "box").put_file("/usr/local/bin/x", b"data", "0755")
    cmd, kw = calls[0]
    argv = cmd[cmd.index("--") + 1:]
    assert argv[:2] == ["sh", "-c"]
    # path and mode are positional args, never part of the script text
    assert argv[3:] == ["sh", "/usr/local/bin/x", "0755"]
    assert "/usr/local/bin/x" not in argv[2] and "0755" not in argv[2]
    assert "/dev/stdin" not in argv[2]
    assert kw["input"] == b"data"


def test_put_file_script_runs_and_sets_mode(tmp_path):
    """Run the real script locally (no incus): contents, mode, no temp left."""
    import os
    import stat
    script = None

    def capture(self, argv, **kw):
        nonlocal script
        script = argv
    orig = sc.Container.root_argv
    sc.Container.root_argv = capture
    try:
        sc.Container("incus", "box").put_file("x", b"data", "0755")
    finally:
        sc.Container.root_argv = orig
    target = tmp_path / "it's a file"
    subprocess.run(script[:3] + ["sh", str(target), "0755"], input=b"data",
                   check=True)
    assert target.read_bytes() == b"data"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o755
    assert os.listdir(tmp_path) == [target.name]


def test_self_check_round_trips_and_detects_mismatch(monkeypatch):
    c = sc.Container("incus", "box")
    seen = []
    monkeypatch.setattr(c, "put_file", lambda *a: seen.append(a))
    monkeypatch.setattr(c, "root_argv", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, stdout=b"ok"))
    c.self_check()
    assert seen and seen[0][1] == b"ok"
    monkeypatch.setattr(c, "root_argv", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, stdout=b""))
    with pytest.raises(RuntimeError, match="self-check"):
        c.self_check()


def test_user_cmd_runs_as_unprivileged_user():
    cmd = sc.Container("incus", "box").user_cmd("bash", "-lc", "id",
                                                env={"TERM": "xterm"})
    assert cmd[:3] == ["incus", "exec", "box"]
    assert cmd[cmd.index("--user") + 1] == sc.UID
    assert f"HOME={sc.HOME}" in cmd and "TERM=xterm" in cmd
    assert cmd[cmd.index("--") + 1:] == ["bash", "-lc", "id"]


def test_wait_network_gives_up(monkeypatch):
    c = sc.Container("incus", "box")
    monkeypatch.setattr(c, "root", lambda *a, **k:
                        subprocess.CompletedProcess(a, 1))
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="network"):
        c.wait_network()
