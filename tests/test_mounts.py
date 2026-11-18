import pytest

from claude_sandbox.container import mounts
from claude_sandbox.container.mounts import BindMount, parse_mount_spec


def test_defaults_are_read_only_under_mnt(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    m = parse_mount_spec(str(d))
    assert m == BindMount(str(d), "/mnt/data", "ro") and m.readonly


def test_explicit_dest_and_modes(tmp_path):
    assert parse_mount_spec(f"{tmp_path}:/srv/x:rw").mode == "rw"
    assert not parse_mount_spec(f"{tmp_path}:/srv/x:rw").readonly
    m = parse_mount_spec(f"{tmp_path}:ro")  # mode without a dest
    assert m.readonly and m.path == f"/mnt/{tmp_path.name}"
    assert parse_mount_spec(f"{tmp_path}:/srv/x:ro").readonly


def test_symlink_source_is_resolved(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    assert parse_mount_spec(str(tmp_path / "link")).source == str(tmp_path / "real")


@pytest.mark.parametrize("bad", [
    "/nonexistent-dir-xyz", "", ":/mnt/x", "{p}:/mnt/x:rw:extra",
    "{p}:relative", "{p}:/", "{p}:/etc", "{p}:/etc/ssh", "{p}:/tmp/x",
    "{p}:/home/ubuntu", "{p}:/home/ubuntu/.ssh", "{p}:/home/ubuntu/.claude/x",
    "{p}:/mnt/a,b"])
def test_rejects_unsafe_specs(tmp_path, bad):
    with pytest.raises(ValueError):
        parse_mount_spec(bad.format(p=tmp_path))


def test_root_source_needs_explicit_dest():
    with pytest.raises(ValueError):
        parse_mount_spec("/")


def test_dotdot_is_normalised_before_checking(tmp_path):
    with pytest.raises(ValueError):
        parse_mount_spec(f"{tmp_path}:/mnt/../etc")


def test_set_rejects_overlap_and_workdir():
    a, b = BindMount("/a", "/mnt/a", "ro"), BindMount("/b", "/mnt/a/b", "ro")
    with pytest.raises(ValueError, match="overlap"):
        mounts.check_set([a, b])
    with pytest.raises(ValueError, match="work directory"):
        mounts.check_set([BindMount("/a", "/home/ubuntu/proj/x", "ro")],
                         "/home/ubuntu/proj")
    assert mounts.check_set([a], "/home/ubuntu/proj") == [a]


def test_section_states_mode_and_source():
    assert mounts.mounts_section([]) == ""
    s = mounts.mounts_section([BindMount("/h/d", "/mnt/d", "ro"),
                               BindMount("/h/w", "/mnt/w", "rw")])
    assert "`/mnt/d`  (host `/h/d`): **read-only**" in s
    assert "`/mnt/w`  (host `/h/w`): **read-write**" in s
    assert "not protected by git" in s
