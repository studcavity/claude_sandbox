"""Tests for sandbox_images: aliases, freshness, pruning, build orchestration."""
import datetime
import json

import pytest

import sandbox_deps
import sandbox_images as im

DAY = datetime.timedelta(days=1)
SHA = "b" * 64


def test_base_alias_is_stable_and_source_dependent():
    a = im.base_alias("ubuntu:24.04")
    assert a == im.base_alias("ubuntu:24.04")
    assert a != im.base_alias("images:ubuntu/24.04")
    assert a.startswith(im.IMAGE_PREFIX)


def test_deps_prefix_does_not_collide_with_base_prefix():
    # base-image cleanup deletes everything under IMAGE_PREFIX
    assert not im.DEPS_IMAGE_PREFIX.startswith(im.IMAGE_PREFIX)
    assert im.deps_alias("base", "k").startswith(im.DEPS_IMAGE_PREFIX)


def test_parse_image_list_filters_by_prefix():
    out = json.dumps([
        {"created_at": "2026-11-14T23:15:00.123456789Z",
         "aliases": [{"name": "claude-sandbox-abc"}, {"name": "other"}]},
        {"created_at": "2026-11-01T00:00:00Z", "aliases": None},
    ])
    got = im.parse_image_list(out, im.IMAGE_PREFIX)
    assert list(got) == ["claude-sandbox-abc"]
    assert got["claude-sandbox-abc"].tzinfo is not None
    assert im.parse_image_list("", im.IMAGE_PREFIX) == {}


def test_is_fresh(now):
    assert not im.is_fresh(None, 7, now=now)
    assert im.is_fresh(now - DAY, 7, now=now)
    assert not im.is_fresh(now - 8 * DAY, 7, now=now)
    # derived image older than its base is stale
    assert not im.is_fresh(now - 2 * DAY, 7, not_older_than=now - DAY, now=now)
    assert im.is_fresh(now - DAY, 7, not_older_than=now - 2 * DAY, now=now)


def test_images_to_prune_keeps_newest_and_current(now):
    imgs = {"cur": now - 10 * DAY, "a": now - 3 * DAY, "b": now - 2 * DAY,
            "c": now - DAY}
    assert im.images_to_prune(imgs, 2, "cur") == ["a", "b"]
    assert im.images_to_prune(imgs, 10, "cur") == []


# --------------------------------------------------------------------------- #
# orchestration (lxc/incus mocked)
# --------------------------------------------------------------------------- #
@pytest.fixture
def cli_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(im, "run", lambda cmd, **kw: calls.append(cmd))
    return calls


@pytest.fixture
def builds(monkeypatch):
    seen = []
    monkeypatch.setattr(im, "build_image",
                        lambda cli, src, alias, prefix, provision, replace=False:
                        seen.append((src, alias, replace)))
    return seen


def test_ensure_base_image_reuses_fresh(monkeypatch, builds, now):
    alias = im.base_alias("src")
    monkeypatch.setattr(im, "cached_images", lambda cli, prefix=None: {
        alias: datetime.datetime.now(datetime.timezone.utc) - DAY})
    assert im.ensure_base_image("incus", "src", 7, rebuild=False) == alias
    assert builds == []


def test_ensure_base_image_rebuilds_and_keeps_other_sources_bases(
        monkeypatch, builds, cli_calls):
    """Another repo's base (a different --image / Ubuntu release) survives."""
    alias = im.base_alias("src")
    old = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(im, "cached_images", lambda cli, prefix=None: {
        alias: old, "claude-sandbox-other-release": old})
    assert im.ensure_base_image("incus", "src", 7, rebuild=False) == alias
    assert builds == [("src", alias, True)]
    assert cli_calls == []


def test_ensure_base_image_bounds_old_bases(monkeypatch, builds, cli_calls):
    alias = im.base_alias("src")
    base = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    stale = {f"claude-sandbox-s{i}": base + datetime.timedelta(days=i)
             for i in range(im.MAX_BASE_IMAGES + 1)}
    monkeypatch.setattr(im, "cached_images", lambda cli, prefix=None: dict(stale))
    im.ensure_base_image("incus", "src", 7, rebuild=False)
    # room for MAX_BASE_IMAGES including the new one: the 2 oldest go
    assert cli_calls == [["incus", "image", "delete", "claude-sandbox-s0"],
                         ["incus", "image", "delete", "claude-sandbox-s1"]]
    assert builds == [("src", alias, False)]


def test_ensure_deps_image_skips_unpinned_scripts(builds):
    deps = sandbox_deps.parse("scripts: [{name: s, url: 'https://x/s'}]")
    assert im.ensure_deps_image("incus", "base", deps, 7, False) is None
    assert builds == []


def test_ensure_deps_image_builds_when_missing(monkeypatch, tmp_path, builds,
                                               cli_calls):
    monkeypatch.setattr(im, "LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(im, "cached_images", lambda cli, prefix=None: {})
    deps = sandbox_deps.parse("apt: [git]")
    alias = im.ensure_deps_image("incus", "base", deps, 7, False)
    assert alias == im.deps_alias("base", sandbox_deps.cache_key(deps))
    assert builds == [("base", alias, False)]


# --------------------------------------------------------------------------- #
# apply_deps
# --------------------------------------------------------------------------- #
def test_apply_deps_order_and_targets(monkeypatch, fake_box):
    deps = sandbox_deps.parse(f"""
apt: [git]
binaries: [{{name: tool, url: 'https://x/t', sha256: {SHA}}}]
scripts: [{{name: inst, url: 'https://x/i.sh', args: [-y], env: {{A: b}}}}]
""")
    fetched = []
    monkeypatch.setattr(sandbox_deps, "fetch",
                        lambda url, cap, sha=None: fetched.append(url) or b"X")
    im.apply_deps(fake_box, deps)

    kinds = [(c[0], c[1]) for c in fake_box.calls]
    assert kinds[0] == ("root_argv", ["apt-get", "update", "-qq"])
    assert kinds[1][1][-1] == "git"
    assert ("put_file", "/usr/local/bin/tool") in kinds
    run_script = next(c for c in fake_box.calls
                      if c[0] == "root_argv" and c[1][0] == "bash")
    assert run_script[1][2:] == ["-y"] and run_script[2] == {"A": "b"}
    # scratch dir is always cleaned up
    assert fake_box.calls[-1][1][:2] == ["rm", "-rf"]
    assert fetched == ["https://x/t", "https://x/i.sh"]


def test_apply_deps_cleans_scratch_on_failure(monkeypatch, fake_box):
    deps = sandbox_deps.parse("scripts: [{name: s, url: 'https://x/s'}]")

    def fail(*a, **k):
        raise sandbox_deps.DepsError("boom")
    monkeypatch.setattr(sandbox_deps, "fetch", fail)
    with pytest.raises(sandbox_deps.DepsError):
        im.apply_deps(fake_box, deps)
    assert fake_box.calls[-1][1][:2] == ["rm", "-rf"]


def test_base_packages_exist_on_ubuntu_26_04():
    """`dnsutils` was dropped from 26.04; bind9-dnsutils works on 24.04 and
    26.04 (checked against both archives' package indexes)."""
    assert "bind9-dnsutils" in im.BASE_PACKAGES
    assert "dnsutils" not in im.BASE_PACKAGES


def test_base_image_prune_keeps_other_sources_recent_bases(now):
    """Repos on different Ubuntu releases must not delete each other's base."""
    imgs = {f"claude-sandbox-{i}": now - datetime.timedelta(days=i)
            for i in range(1, 4)}
    imgs["claude-sandbox-new"] = now
    assert im.images_to_prune(imgs, im.MAX_BASE_IMAGES, "claude-sandbox-new") == []
    imgs["claude-sandbox-old"] = now - datetime.timedelta(days=30)
    assert im.images_to_prune(imgs, im.MAX_BASE_IMAGES,
                              "claude-sandbox-new") == ["claude-sandbox-old"]
