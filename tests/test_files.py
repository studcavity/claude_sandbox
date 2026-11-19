"""Tests for container/files.py: approval, pin checks, placement, on_error."""
import hashlib

import pytest

from claude_sandbox import sandbox
from claude_sandbox.container import files as f
from claude_sandbox.container.deps import DepsError

DATA = b"hello\n"
SHA = hashlib.sha256(DATA).hexdigest()


def entry(**kw):
    e = {"name": "cfg", "dest": "~/.config/tool/cfg", "url": "https://x.test/c",
         "host_path": None, "sha256": SHA, "mode": "0644", "on_error": "fail"}
    e.update(kw)
    return e


def host_entry(**kw):
    kw = {"name": "host", "dest": "~/host", **kw}
    return entry(url=None, host_path="/host/c", **kw)


def logs():
    out = []
    return out, out.append


def test_url_entries_are_granted_host_paths_need_approval():
    u, h = entry(), host_entry(name="h", dest="~/h")
    assert f.resolve([u, h], False, None) == ([u], [h])        # no tty, no flag
    assert f.resolve([u, h], True, None) == ([u, h], [])       # --allow-files
    assert f.resolve([u, h], False, lambda e: True) == ([u, h], [])
    assert f.resolve([u, h], False, lambda e: False) == ([u], [h])


def test_place_writes_verified_bytes_under_home(fake_box):
    notes = f.place(fake_box, [entry(mode="0600")], "/home/ubuntu/repo",
                    lambda m: None, fetch=lambda url, cap, sha: DATA)
    assert fake_box.calls == [("put_user_file", ".config/tool/cfg", DATA, "0600")]
    assert "placed" in notes[0] and "not placed" not in notes[0]


def test_host_file_must_match_its_pin(fake_box):
    ok = f.place(fake_box, [host_entry()], None, lambda m: None,
                 read=lambda p: DATA)
    assert ok and fake_box.calls
    fake_box.calls.clear()
    with pytest.raises(DepsError, match="sha256 mismatch"):
        f.place(fake_box, [host_entry()], None, lambda m: None,
                read=lambda p: DATA + b"tampered")
    assert fake_box.calls == []  # nothing reached the container


def test_on_error_fail_aborts_and_skip_continues(fake_box):
    def boom(url, cap, sha):
        raise DepsError("download failed")

    with pytest.raises(DepsError, match="files: cfg: download failed"):
        f.place(fake_box, [entry()], None, lambda m: None, fetch=boom)
    out, log = logs()
    notes = f.place(fake_box, [entry(on_error="skip"),
                               entry(name="b", dest="~/b", url="https://x.test/b")],
                    None, log,
                    fetch=lambda u, c, s: boom(u, c, s) if u.endswith("c") else DATA)
    assert "placed" in notes[1] and "not placed" not in notes[1]
    assert "not placed" in notes[0] and "download failed" in notes[0]
    assert out and "continuing" in out[0]


def test_skip_also_covers_a_failed_write_but_fail_does_not(fake_box):
    import subprocess

    def bad(rel, data, mode):
        raise subprocess.CalledProcessError(1, "x")

    fake_box.put_user_file = bad
    fetch = lambda u, c, s: DATA  # noqa: E731
    notes = f.place(fake_box, [entry(on_error="skip")], None, lambda m: None,
                    fetch=fetch)
    assert "not placed" in notes[0]
    with pytest.raises(subprocess.CalledProcessError):
        f.place(fake_box, [entry()], None, lambda m: None, fetch=fetch)


@pytest.mark.parametrize("dest", ["~/repo", "~/repo/x"])
def test_destination_may_not_overlap_the_work_directory(dest, fake_box):
    with pytest.raises(DepsError, match="overlaps"):
        f.place(fake_box, [entry(dest=dest)], "/home/ubuntu/repo/x"
                if dest == "~/repo" else "/home/ubuntu/repo", lambda m: None,
                fetch=lambda u, c, s: DATA)
    assert fake_box.calls == []


def test_destination_outside_workdir_is_fine():
    assert f.check_dest(entry(dest="~/repo2/x"), "/home/ubuntu/repo") == \
        "/home/ubuntu/repo2/x"


def test_read_host_file(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(DATA)
    assert f.read_host_file(str(p)) == DATA
    with pytest.raises(DepsError, match="larger"):
        f.read_host_file(str(p), max_bytes=2)
    with pytest.raises(DepsError, match="not a regular file"):
        f.read_host_file(str(tmp_path))
    with pytest.raises(DepsError, match="can't read"):
        f.read_host_file(str(tmp_path / "missing"))


# --- Sandbox wiring -------------------------------------------------------- #
class _Tty:
    def isatty(self):
        return True


def _box(monkeypatch, make_args, downloads=(), host_files=(), **kw):
    from claude_sandbox.container import deps as cd
    box = sandbox.Sandbox(make_args(repo="o/r", **kw))
    box.deps = dict(cd.parse(""), downloads=list(downloads))
    monkeypatch.setattr(box, "host_config", lambda: {"files": list(host_files)})
    return box


def test_resolve_files_flags(monkeypatch, make_args):
    h = host_entry()
    monkeypatch.setattr(sandbox, "ask_file", lambda e: pytest.fail("asked"))
    box = _box(monkeypatch, make_args, [entry()], [h])
    assert box.resolve_files() == [entry()]            # no tty: host file skipped
    assert box.declined_files == [h]
    box = _box(monkeypatch, make_args, [entry()], [h], allow_files=True)
    assert box.resolve_files() == [entry(), h]
    box = _box(monkeypatch, make_args, [entry()], [h], no_files=True,
               allow_files=True)
    assert box.resolve_files() == [entry()] and "--no-files" in box.deps_note


def test_resolve_files_prompts_on_a_tty(monkeypatch, make_args):
    monkeypatch.setattr(sandbox.sys, "stdin", _Tty())
    asked = []
    monkeypatch.setattr(sandbox, "ask_file",
                        lambda e: asked.append(e["name"]) or False)
    box = _box(monkeypatch, make_args, [entry()],
               [host_entry(name="h", dest="~/h")])
    box.resolve_files()
    assert asked == ["h"] and [e["name"] for e in box.declined_files] == ["h"]


def test_fail_entries_are_baked_with_the_deps_skip_entries_are_not(
        monkeypatch, make_args):
    from claude_sandbox.container import deps as cd
    box = _box(monkeypatch, make_args,
               [entry(), entry(name="s", dest="~/s", on_error="skip")],
               [host_entry(name="h", dest="~/h")], allow_files=True)
    before = cd.cache_key(box.deps)
    box.resolve_files()
    assert [e["name"] for e in box.deps["files"]] == ["cfg", "h"]
    assert [e["name"] for e in box.launch_files] == ["s"]
    assert cd.cache_key(box.deps) not in (None, before)
    # an unapproved host file is never in the key, so can't be in the image
    box2 = _box(monkeypatch, make_args, [entry()], [host_entry(name="h", dest="~/h")])
    box2.resolve_files()
    assert [e["name"] for e in box2.deps["files"]] == ["cfg"]
    assert cd.cache_key(box2.deps) != cd.cache_key(box.deps)


def test_host_files_alone_still_get_a_deps_image(monkeypatch, make_args):
    box = _box(monkeypatch, make_args, [], [host_entry()], allow_files=True)
    box.deps = None   # no deps file at all
    box.resolve_files()
    assert box.deps and [e["name"] for e in box.deps["files"]] == ["host"]


def test_overlap_with_the_clone_fails_or_skips_per_on_error(monkeypatch, make_args):
    box = _box(monkeypatch, make_args, [entry(dest="~/r")])
    monkeypatch.setattr(box.backend, "workdir", lambda: "/home/ubuntu/r")
    with pytest.raises(DepsError, match="overlaps"):
        box.resolve_files()
    box = _box(monkeypatch, make_args, [entry(dest="~/r", on_error="skip")])
    monkeypatch.setattr(box.backend, "workdir", lambda: "/home/ubuntu/r")
    assert box.resolve_files() == []
    assert "overlaps" in box.declined_files[0]["reason"]


def test_place_files_reports_to_the_session(monkeypatch, make_args, fake_box):
    from claude_sandbox.container import deps as cd
    monkeypatch.setattr(cd, "fetch", lambda url, cap, sha: DATA)
    box = _box(monkeypatch, make_args,
               [entry(on_error="skip")], [host_entry(name="h", dest="~/h")])
    box.box = fake_box
    box.resolve_files()
    box.place_files("/home/ubuntu/repo")
    assert [c[1] for c in fake_box.calls] == [".config/tool/cfg"]
    assert "`~/.config/tool/cfg`: placed" in box.deps_note
    assert "`~/h`: **not placed**" in box.deps_note and "--allow-files" in box.deps_note


def test_apply_deps_installs_baked_files(monkeypatch, fake_box):
    from claude_sandbox.container import deps as cd
    from claude_sandbox.container.images import apply_deps
    monkeypatch.setattr(cd, "fetch", lambda url, cap, sha: DATA)
    monkeypatch.setattr(f, "read_host_file", lambda p: DATA)
    apply_deps(fake_box, dict(cd.parse(""), files=[
        entry(mode="0600"), host_entry(name="h", dest="~/h")]))
    assert fake_box.calls == [
        ("put_user_file", ".config/tool/cfg", DATA, "0600"),
        ("put_user_file", "h", DATA, "0644")]
