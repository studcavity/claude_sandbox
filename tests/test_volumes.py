"""Tests for sandbox_volumes: planning, orphan detection, attach/delete."""
import datetime
import json
import subprocess

import pytest

from claude_sandbox.container import volumes as sv
NOW = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc)


def test_plan_disabled_without_size():
    assert sv.plan("c", "/home/ubuntu/p", None) == []


def test_plan_covers_tmp_and_clone_dir():
    m = sv.plan("c1", "/home/ubuntu/proj", "20GiB")
    assert [x.path for x in m] == ["/tmp", "/home/ubuntu/proj"]
    assert m[0].mode == "1777" and m[1].owner == "ubuntu:ubuntu"
    assert all(x.volume.startswith(sv.VOLUME_PREFIX + "c1-") for x in m)


def test_plan_leaves_bare_home_on_root_disk():
    assert [x.path for x in sv.plan("c", "/home/ubuntu", "20GiB")] == ["/tmp"]


def _vol(name, used_by=(), age_h=2, typ="custom", created=None):
    t = created or (NOW - datetime.timedelta(hours=age_h)).strftime(
        "%Y-%m-%dT%H:%M:%S.123Z")
    return {"name": name, "type": typ, "used_by": list(used_by),
            "created_at": t}


def test_orphans_only_old_unused_ours():
    vols = [_vol(sv.VOLUME_PREFIX + "leaked"),
            _vol(sv.VOLUME_PREFIX + "inuse", used_by=["/1.0/instances/x"]),
            _vol(sv.VOLUME_PREFIX + "fresh", age_h=0),
            _vol("someone-elses"),
            _vol(sv.VOLUME_PREFIX + "image", typ="image"),
            _vol(sv.VOLUME_PREFIX + "badtime", created="garbage")]
    assert sv.orphans(json.dumps(vols), NOW) == [sv.VOLUME_PREFIX + "leaked"]
    assert sv.orphans("", NOW) == []


class Box:
    name = "c1"

    def __init__(self):
        self.calls = []

    def root_argv(self, argv, **kw):
        self.calls.append(argv)


@pytest.fixture
def cli(monkeypatch):
    calls = []

    def fake(cmd, check=True, capture=False, input=None):
        calls.append(cmd)
        out = ""
        if cmd[1:3] == ["profile", "device"]:
            out = "default\n"
        if cmd[1:3] == ["storage", "volume"] and cmd[3] == "list":
            out = json.dumps([_vol(sv.VOLUME_PREFIX + "old",
                                  created="2000-01-01T00:00:00Z")])
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(sv, "run", fake)
    monkeypatch.setattr(sv.time, "sleep", lambda s: None)
    return calls


def test_attach_creates_mounts_and_fixes_perms_then_delete(cli):
    box = Box()
    m = sv.plan("c1", "/home/ubuntu/p", "20GiB")
    v = sv.Volumes("incus", box, m, "20GiB")
    v.attach()
    assert ["incus", "storage", "volume", "delete", "default",
            sv.VOLUME_PREFIX + "old"] in cli  # swept the leak
    assert ["incus", "storage", "volume", "create", "default",
            m[0].volume, "size=20GiB"] in cli
    assert ["incus", "config", "device", "add", "c1", "cs-work", "disk",
            "pool=default", f"source={m[1].volume}",
            "path=/home/ubuntu/p"] in cli
    assert ["chmod", "1777", "/tmp"] in box.calls
    assert ["chown", "ubuntu:ubuntu", "/home/ubuntu/p"] in box.calls
    v.delete()
    assert ["incus", "storage", "volume", "delete", "default",
            m[1].volume] in cli and v.created == []


def test_attach_failure_degrades_to_root_disk(monkeypatch, capsys):
    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="no quota")
    monkeypatch.setattr(sv, "run", boom)
    v = sv.Volumes("incus", Box(), sv.plan("c", "/home/ubuntu/p", "1GiB"), "1GiB")
    v.attach()  # must not raise
    assert v.created == []
    assert "continuing with everything on the root disk" in capsys.readouterr().err


def test_delete_retries_while_instance_is_going_away(monkeypatch):
    rcs = iter([1, 1, 0])
    monkeypatch.setattr(sv, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, next(rcs), "", ""))
    monkeypatch.setattr(sv.time, "sleep", lambda s: None)
    v = sv.Volumes("incus", Box(), [], "1GiB")
    v.pool, v.created = "default", ["vol"]
    v.delete()
    assert v.created == []


def test_delete_says_when_a_volume_is_gone(monkeypatch, capsys):
    monkeypatch.setattr(sv, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, "", ""))
    v = sv.Volumes("incus", Box(), [], "1GiB")
    v.pool, v.created = "default", ["vol"]
    v.delete()
    assert "deleted volume vol" in capsys.readouterr().err


# --- kept work volumes (local backend, spec 08) -------------------------------
def test_a_kept_work_volume_is_named_apart_and_labelled():
    m = sv.plan("c1", "/home/ubuntu/p", "20GiB", keep_work={"session": "s", "base": "ab"})
    tmp, work = m
    assert tmp.volume.startswith(sv.VOLUME_PREFIX) and not tmp.keep
    assert work.volume.startswith(sv.KEEP_PREFIX) and work.keep
    assert work.config == ((sv.META_PREFIX + "base", "ab"),
                           (sv.META_PREFIX + "session", "s"))


def test_the_sweep_never_takes_a_kept_volume(now):
    vols = json.dumps([_vol(sv.KEEP_PREFIX + "c-work", age_h=1000)])
    assert sv.orphans(vols, NOW) == []


def test_delete_leaves_kept_volumes_until_released(cli):
    m = sv.plan("c1", "/home/ubuntu/p", "20GiB", keep_work={"session": "s"})
    v = sv.Volumes("incus", Box(), m, "20GiB")
    v.attach()
    assert ["incus", "storage", "volume", "create", "default", m[1].volume,
            "size=20GiB", sv.META_PREFIX + "session=s"] in cli
    v.delete()
    deleted = [c[-1] for c in cli if c[1:4] == ["storage", "volume", "delete"]]
    assert m[0].volume in deleted and m[1].volume not in deleted
    assert v.created == [m[1].volume]
    v.release()
    assert ["incus", "storage", "volume", "delete", "default", m[1].volume] in cli
    assert v.created == []
