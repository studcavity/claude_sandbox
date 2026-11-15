"""Tests for pool: parsing, thresholds, advisory behaviour."""
import subprocess

import pytest

from claude_sandbox.container import pool as sp
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


# --- real incus output (captured 2026-11-15 from `incus storage info default --bytes`) -- #
REAL_DIR = '''info:
  description: ""
  driver: dir
  name: default
  space used: "15544856576"
  total space: "64424509440"
used by:
  profiles:
  - default
'''


def test_parse_space_reads_the_real_nested_format():
    """Regression: the parser only knew a flat mapping, so on real incus it
    returned None and a full pool was reported as 'ok' (the check never ran)."""
    assert sp.parse_space(REAL_DIR) == (15544856576, 64424509440)
    assert sp.assess(sp.parse_space(REAL_DIR)) == "ok"
    full = REAL_DIR.replace("15544856576", str(int(63.5 * GiB)))
    assert sp.assess(sp.parse_space(full)) == "full"


@pytest.mark.parametrize("text,driver", [
    (REAL_DIR, "dir"), (REAL_DIR.replace("driver: dir", "driver: zfs"), "zfs"),
    ("driver: btrfs\nspace used: 1", "btrfs"),          # flat form
    ("info:\n  name: x", None), ("", None), ("- a\n- b", None), (": [", None),
    ("info:\n  driver: [1]", None),
])
def test_parse_driver(text, driver):
    assert sp.parse_driver(text) == driver


def test_driver_advice_only_for_dir():
    assert "copies the whole image" in sp.driver_advice("default", "dir")
    for d in ("zfs", "btrfs", "lvm", "ceph", None):
        assert sp.driver_advice("default", d) is None


def test_check_notes_a_dir_pool_but_never_blocks_on_it(monkeypatch, capsys):
    _cli(monkeypatch, REAL_DIR)
    assert sp.check("incus") is None
    assert "uses the `dir` driver" in capsys.readouterr().err


def test_check_is_quiet_for_a_copy_on_write_pool(monkeypatch, capsys):
    _cli(monkeypatch, REAL_DIR.replace("driver: dir", "driver: zfs"))
    assert sp.check("incus") is None
    assert "driver" not in capsys.readouterr().err


def test_a_full_real_pool_now_blocks_the_launch(monkeypatch):
    _cli(monkeypatch, REAL_DIR.replace("15544856576", str(int(63.5 * GiB))))
    assert "has only" in sp.check("incus")
