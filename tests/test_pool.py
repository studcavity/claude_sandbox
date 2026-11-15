"""Tests for sandbox_pool: parsing, thresholds, advisory behaviour."""
import subprocess

import pytest

import sandbox_pool as sp

GiB = sp.GiB


def test_parse_space():
    assert sp.parse_space("space used: 100\ntotal space: 1000\nx: y") == (100, 1000)
    for junk in ("", "info: {}", "space used: lots\ntotal space: 1", ": [", None):
        assert sp.parse_space(junk) is None


@pytest.mark.parametrize("used,total,state", [
    (9.6, 10, "full"), (7.5, 10, "full"), (4, 10, "warn"), (20, 30, "warn"), (10, 50, "ok")])
def test_assess(used, total, state):
    assert sp.assess((int(used * GiB), int(total * GiB))) == state
    assert sp.assess(None) == "ok"


def _cli(monkeypatch, info, calls=None):
    def fake(cmd, check=True, capture=False, input=None):
        (calls if calls is not None else []).append(cmd)
        out = "default\n" if cmd[1] == "profile" else info
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(sp, "run", fake)


def test_check_errors_with_grow_command_when_full(monkeypatch):
    _cli(monkeypatch, f"space used: {int(9.6 * GiB)}\ntotal space: {10 * GiB}")
    msg = sp.check("incus")
    assert "incus storage set default size=50GiB" in msg and "10GiB" in msg


def test_check_warns_but_proceeds_when_low(monkeypatch, capsys):
    _cli(monkeypatch, f"space used: {20 * GiB}\ntotal space: {30 * GiB}")
    assert sp.check("incus") is None
    assert "warning" in capsys.readouterr().err


def test_check_grows_first_when_asked(monkeypatch):
    calls = []
    _cli(monkeypatch, f"space used: {GiB}\ntotal space: {50 * GiB}", calls)
    assert sp.check("incus", grow_to="50GiB") is None
    assert ["incus", "storage", "set", "default", "size=50GiB"] in calls


def test_check_is_advisory_on_cli_failure(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(sp, "run", boom)
    assert sp.check("incus") is None
