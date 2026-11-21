"""Tests for images: aliases, freshness, pruning, build orchestration."""
import datetime
import json

import pytest

from claude_sandbox.container import deps as container_deps
from claude_sandbox.container import images as im
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
                        lambda cli, src, alias, prefix, provision, replace=False,
                        root_size=None:
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
    deps = container_deps.parse("scripts: [{name: s, url: 'https://x/s'}]")
    assert im.ensure_deps_image("incus", "base", deps, 7, False) is None
    assert builds == []


def test_ensure_deps_image_builds_when_missing(monkeypatch, tmp_path, builds,
                                               cli_calls):
    monkeypatch.setattr(im, "LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(im, "cached_images", lambda cli, prefix=None: {})
    deps = container_deps.parse("apt: [git]")
    alias = im.ensure_deps_image("incus", "base", deps, 7, False)
    assert alias == im.deps_alias("base", container_deps.cache_key(deps))
    assert builds == [("base", alias, False)]


# --------------------------------------------------------------------------- #
# apply_deps
# --------------------------------------------------------------------------- #
def test_apply_deps_order_and_targets(monkeypatch, fake_box):
    deps = container_deps.parse(f"""
apt: [git]
binaries: [{{name: tool, url: 'https://x/t', sha256: {SHA}}}]
scripts: [{{name: inst, url: 'https://x/i.sh', args: [-y], env: {{A: b}}}}]
""")
    fetched = []
    monkeypatch.setattr(container_deps, "fetch",
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
    deps = container_deps.parse("scripts: [{name: s, url: 'https://x/s'}]")

    def fail(*a, **k):
        raise container_deps.DepsError("boom")
    monkeypatch.setattr(container_deps, "fetch", fail)
    with pytest.raises(container_deps.DepsError):
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


def test_build_image_caps_root_disk_of_build_container(monkeypatch):
    launches = []
    class B:
        def __init__(self, cli, name): self.name = name
        def launch(self, source, ephemeral, root_size=None):
            launches.append(root_size)
        def wait_network(self): pass
        def root(self, *a, **kw): pass
        def delete(self): pass
    monkeypatch.setattr(im, "Container", B)
    monkeypatch.setattr(im, "run", lambda *a, **kw: None)
    im.build_image("incus", "src", "a", "p-", lambda b: None, root_size="30GiB")
    assert launches == ["30GiB"]


def test_base_setup_caps_journal():
    assert "SystemMaxUse=100M" in im.BASE_ROOT_SETUP


def test_apply_deps_verifies_the_attestation_before_installing(monkeypatch, fake_box):
    deps = container_deps.parse(
        f"binaries: [{{name: tool, url: 'https://x/t', sha256: {SHA}, attest: o/r}}]")
    monkeypatch.setattr(container_deps, "fetch", lambda *a, **k: b"X")
    seen = []
    im.apply_deps(fake_box, deps, lambda data, repo: seen.append((data, repo)))
    assert seen == [(b"X", "o/r")]
    assert ("put_file", "/usr/local/bin/tool") in [(c[0], c[1]) for c in fake_box.calls]


def test_a_failed_attestation_installs_nothing(monkeypatch, fake_box):
    deps = container_deps.parse(
        f"binaries: [{{name: tool, url: 'https://x/t', sha256: {SHA}, attest: o/r}}]")
    monkeypatch.setattr(container_deps, "fetch", lambda *a, **k: b"X")

    def bad(data, repo):
        raise RuntimeError("no attestation")
    with pytest.raises(RuntimeError):
        im.apply_deps(fake_box, deps, bad)
    assert not any(c[0] == "put_file" for c in fake_box.calls)


def test_an_attest_entry_without_a_verifier_is_refused(monkeypatch, fake_box):
    deps = container_deps.parse(
        f"binaries: [{{name: tool, url: 'https://x/t', sha256: {SHA}, attest: o/r}}]")
    monkeypatch.setattr(container_deps, "fetch", lambda *a, **k: b"X")
    with pytest.raises(container_deps.DepsError, match="attestation"):
        im.apply_deps(fake_box, deps)   # never silently skipped
    assert not any(c[0] == "put_file" for c in fake_box.calls)


REPO_YAML = (f"apt_repos: [{{name: gh, url: 'https://cli.github.com/packages', "
             f"suite: stable, architectures: [amd64], key_url: "
             f"'https://cli.github.com/packages/k.gpg', key_sha256: {SHA}}}]\n")


def test_apt_source_text_is_deb822_with_signed_by():
    r, = container_deps.parse(REPO_YAML)["apt_repos"]
    assert im.apt_source_text(r) == (
        "Types: deb\nURIs: https://cli.github.com/packages\nSuites: stable\n"
        "Components: main\nArchitectures: amd64\n"
        "Signed-By: /etc/apt/keyrings/claude-sandbox-gh.gpg\n")


def test_apt_repos_are_added_then_apt_is_updated_before_installing(
        monkeypatch, fake_box):
    deps = container_deps.parse(REPO_YAML + "apt: [gh]\n")
    fetched = []
    monkeypatch.setattr(container_deps, "fetch",
                        lambda url, cap, sha=None: fetched.append((url, sha)) or b"KEY")
    im.apply_deps(fake_box, deps)
    assert fetched == [("https://cli.github.com/packages/k.gpg", SHA)]
    kinds = [(c[0], c[1]) for c in fake_box.calls]
    key = kinds.index(("put_file", "/etc/apt/keyrings/claude-sandbox-gh.gpg"))
    src = kinds.index(("put_file",
                       "/etc/apt/sources.list.d/claude-sandbox-gh.sources"))
    update = kinds.index(("root_argv", ["apt-get", "update", "-qq"]))
    install = next(i for i, k in enumerate(kinds)
                   if k[0] == "root_argv" and k[1][:2] == ["apt-get", "install"])
    assert key < src < update < install   # update sees the new repo


def test_a_repo_with_no_packages_still_updates_apt(monkeypatch, fake_box):
    monkeypatch.setattr(container_deps, "fetch", lambda *a, **k: b"KEY")
    im.apply_deps(fake_box, container_deps.parse(REPO_YAML))
    assert ("root_argv", ["apt-get", "update", "-qq"]) in [
        (c[0], c[1]) for c in fake_box.calls]


def test_a_key_that_fails_its_pin_adds_no_source(monkeypatch, fake_box):
    def bad(*a, **k):
        raise container_deps.DepsError("sha256 mismatch")
    monkeypatch.setattr(container_deps, "fetch", bad)
    with pytest.raises(container_deps.DepsError):
        im.apply_deps(fake_box, container_deps.parse(REPO_YAML + "apt: [gh]\n"))
    assert not any(c[0] == "put_file" for c in fake_box.calls)
