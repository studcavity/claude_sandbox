"""Stale-while-revalidate for cached images (plan 1c)."""
import contextlib
import datetime
import subprocess
import types

import pytest

import claude_sandbox as cs
import sandbox_deps
import sandbox_images as im

DAY = datetime.timedelta(days=1)
UTC = datetime.timezone.utc


def ago(days):
    return datetime.datetime.now(UTC) - days * DAY


# --- classify -------------------------------------------------------------------- #
@pytest.mark.parametrize("age,max_age,hard,expect", [
    (None, 7, 30, "missing"),
    (1, 7, 30, "fresh"), (6.9, 7, 30, "fresh"),
    (8, 7, 30, "stale"), (29, 7, 30, "stale"),
    (31, 7, 30, "expired"), (30, 7, 30, "expired"),
    (8, 7, None, "expired"),            # no hard cap: nothing is ever merely stale (legacy)
    (8, 7, 0, "expired"),
    (8, 7, 3, "expired"),               # a hard cap below max_age is clamped UP to it:
    (3, 7, 3, "fresh"),                 # no stale window, so it behaves like no cap
])
def test_classify_by_age(age, max_age, hard, expect):
    created = None if age is None else ago(age)
    assert im.classify(created, max_age, hard) == expect


def test_older_than_the_base_is_stale_not_expired():
    base, deps = ago(1), ago(3)
    assert im.classify(deps, 7, 30, not_older_than=base) == "stale"
    assert im.classify(deps, 7, None, not_older_than=base) == "stale"      # legacy rebuilds on it
    assert im.classify(ago(40), 7, 30, not_older_than=base) == "expired"
    assert im.classify(ago(0.5), 7, 30, not_older_than=base) == "fresh"


def test_classify_agrees_with_is_fresh():
    for age in (0.1, 6, 8, 40):
        for nof in (None, ago(1), ago(10)):
            c = ago(age)
            assert (im.classify(c, 7, 30, nof) == "fresh") == im.is_fresh(c, 7, nof)


# --- base image ----------------------------------------------------------------- #
@pytest.fixture
def builds(monkeypatch):
    seen = []
    monkeypatch.setattr(im, "build_image", lambda cli, src, alias, prefix, provision,
                        replace=False, root_size=None: seen.append((alias, replace)))
    monkeypatch.setattr(im, "delete_images", lambda *a, **k: None)
    return seen


@pytest.fixture
def lock(monkeypatch):
    """Records whether the build lock was taken."""
    taken = []

    @contextlib.contextmanager
    def fake(alias):
        taken.append(alias)
        yield
    monkeypatch.setattr(im, "build_lock", fake)
    return taken


def images(monkeypatch, base=None, deps=None, alias=None, dalias=None):
    def listing(cli, prefix=None):
        if prefix == im.DEPS_IMAGE_PREFIX:
            return {dalias: deps} if deps is not None else {}
        return {alias: base} if base is not None else {}
    monkeypatch.setattr(im, "cached_images", listing)


def test_a_stale_base_is_used_at_once_without_taking_the_lock(monkeypatch, builds, lock):
    alias = im.base_alias("src")
    images(monkeypatch, base=ago(10), alias=alias)
    got = []
    assert im.ensure_base_image("incus", "src", 7, False, hard_max_age_days=30,
                                on_stale=lambda k, a: got.append((k, a))) == alias
    assert got == [("base", alias)]
    assert builds == [] and lock == []        # nobody waits behind a refresher


def test_without_a_callback_a_stale_base_is_rebuilt_as_before(monkeypatch, builds, lock):
    alias = im.base_alias("src")
    images(monkeypatch, base=ago(10), alias=alias)
    im.ensure_base_image("incus", "src", 7, False, hard_max_age_days=30)
    assert builds == [(alias, True)] and lock == [alias]


def test_an_expired_base_is_rebuilt_even_with_a_callback(monkeypatch, builds, lock):
    alias = im.base_alias("src")
    images(monkeypatch, base=ago(45), alias=alias)
    im.ensure_base_image("incus", "src", 7, False, hard_max_age_days=30,
                         on_stale=lambda k, a: pytest.fail("used an expired image"))
    assert builds == [(alias, True)]


def test_a_fresh_base_is_reused_without_the_lock_or_a_callback(monkeypatch, builds, lock):
    alias = im.base_alias("src")
    images(monkeypatch, base=ago(1), alias=alias)
    assert im.ensure_base_image("incus", "src", 7, False, hard_max_age_days=30,
                                on_stale=lambda k, a: pytest.fail("not stale")) == alias
    assert builds == [] and lock == []


def test_the_refresher_rebuilds_a_stale_base_under_the_lock(monkeypatch, builds, lock):
    alias = im.base_alias("src")
    images(monkeypatch, base=ago(10), alias=alias)
    im.ensure_base_image("incus", "src", 7, False, hard_max_age_days=30, refresh=True)
    assert builds == [(alias, True)] and lock == [alias]


def test_a_second_refresher_finds_it_already_refreshed(monkeypatch, builds, lock):
    """Two refreshers, or a refresher and a launch: the loser rechecks under the
    lock, sees a fresh image and builds nothing."""
    alias = im.base_alias("src")
    images(monkeypatch, base=ago(0.01), alias=alias)
    assert im.ensure_base_image("incus", "src", 7, False, hard_max_age_days=30,
                                refresh=True) == alias
    assert builds == [] and lock == [alias]


def test_rebuild_flag_always_rebuilds(monkeypatch, builds, lock):
    alias = im.base_alias("src")
    images(monkeypatch, base=ago(1), alias=alias)
    im.ensure_base_image("incus", "src", 7, True, hard_max_age_days=30,
                         on_stale=lambda k, a: pytest.fail("rebuild means rebuild"))
    assert builds == [(alias, True)]


# --- deps image ------------------------------------------------------------------ #
DEPS = sandbox_deps.parse("apt: [git]")


def deps_setup(monkeypatch, base_age, deps_age):
    base = im.base_alias("src")
    dalias = im.deps_alias(base, sandbox_deps.cache_key(DEPS))
    images(monkeypatch, base=ago(base_age), deps=ago(deps_age) if deps_age is not None else None,
           alias=base, dalias=dalias)
    return base, dalias


def test_a_deps_image_older_than_a_refreshed_base_is_used_and_refreshed(monkeypatch, builds, lock):
    """The chain: once the base is refreshed, deps images are older than it. The
    next launch must not block on rebuilding them."""
    base, dalias = deps_setup(monkeypatch, base_age=0.1, deps_age=3)
    got = []
    assert im.ensure_deps_image("incus", base, DEPS, 7, False, hard_max_age_days=30,
                                on_stale=lambda k, a: got.append((k, a))) == dalias
    assert got == [("deps", dalias)] and builds == [] and lock == []


def test_without_a_callback_that_deps_image_is_rebuilt_as_before(monkeypatch, builds, lock):
    base, dalias = deps_setup(monkeypatch, base_age=0.1, deps_age=3)
    im.ensure_deps_image("incus", base, DEPS, 7, False)
    assert builds == [(dalias, True)]


def test_an_expired_deps_image_is_rebuilt(monkeypatch, builds, lock):
    base, dalias = deps_setup(monkeypatch, base_age=0.1, deps_age=40)
    im.ensure_deps_image("incus", base, DEPS, 7, False, hard_max_age_days=30,
                         on_stale=lambda k, a: pytest.fail("expired"))
    assert builds == [(dalias, True)]


def test_a_fresh_deps_image_is_reused_and_a_missing_one_is_built(monkeypatch, builds, lock):
    base, dalias = deps_setup(monkeypatch, base_age=2, deps_age=1)
    assert im.ensure_deps_image("incus", base, DEPS, 7, False, hard_max_age_days=30) == dalias
    assert builds == []
    base, dalias = deps_setup(monkeypatch, base_age=2, deps_age=None)
    im.ensure_deps_image("incus", base, DEPS, 7, False, hard_max_age_days=30,
                         on_stale=lambda k, a: pytest.fail("missing is not stale"))
    assert builds == [(dalias, False)]


def test_the_refresher_rebuilds_a_stale_deps_image(monkeypatch, builds, lock):
    base, dalias = deps_setup(monkeypatch, base_age=0.1, deps_age=3)
    im.ensure_deps_image("incus", base, DEPS, 7, False, hard_max_age_days=30, refresh=True)
    assert builds == [(dalias, True)]


# --- the launcher side ------------------------------------------------------------- #
def args(**kw):
    base = dict(cli="incus", max_age=7.0, hard_max_age=30.0, repo="o/r", image=None,
                disk_size=None, no_deps=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_refresh_argv_carries_what_decides_the_image_names():
    cmd = cs.refresh_argv(args(image="images:ubuntu/24.04", disk_size="30GiB",
                               no_deps=True), "py", "/x/claude_sandbox.py")
    assert cmd[:3] == ["py", "/x/claude_sandbox.py", "--refresh-images"]
    for pair in (["--cli", "incus"], ["--max-age", "7.0"], ["--hard-max-age", "30.0"],
                 ["--repo", "o/r"], ["--image", "images:ubuntu/24.04"],
                 ["--disk-size", "30GiB"]):
        i = cmd.index(pair[0])
        assert cmd[i:i + 2] == pair
    assert "--no-deps" in cmd
    plain = cs.refresh_argv(args(repo=None), "py", "s")
    assert "--repo" not in plain and "--image" not in plain and "--no-deps" not in plain


def test_the_refresher_is_detached_and_logs_to_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    seen = {}
    monkeypatch.setattr(cs.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, **kw))
    cs.spawn_refresh(args())
    assert seen["start_new_session"] is True            # survives the launcher exiting
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["stderr"] == subprocess.STDOUT
    assert "--refresh-images" in seen["cmd"]
    assert (tmp_path / "claude_sandbox" / "image-refresh.log").exists()


def test_a_failure_to_spawn_is_only_a_warning(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    def boom(*a, **k):
        raise OSError("no fork")
    monkeypatch.setattr(cs.subprocess, "Popen", boom)
    cs.spawn_refresh(args())
    assert "couldn't start the background image refresh" in capsys.readouterr().err


def test_refresh_images_refreshes_base_then_deps(monkeypatch, make_args):
    order = []
    monkeypatch.setattr(cs.Sandbox, "load_deps",
                        lambda self: setattr(self, "deps", DEPS))
    monkeypatch.setattr(cs.Sandbox, "choose_image", lambda self: "src")
    monkeypatch.setattr(cs, "ensure_base_image",
                        lambda *a, **k: order.append(("base", k)) or "img")
    monkeypatch.setattr(cs, "ensure_deps_image",
                        lambda cli, image, *a, **k: order.append(("deps", image, k)))
    assert cs.refresh_images(make_args(repo="o/r")) == 0
    assert [o[0] for o in order] == ["base", "deps"]
    assert order[0][1]["refresh"] is True and order[1][2]["refresh"] is True
    assert order[1][1] == "img"                          # deps built from the refreshed base


def test_refresh_images_reports_failure_without_raising(monkeypatch, make_args):
    def boom(self):
        raise cs.GitHubError("no auth", 0)
    monkeypatch.setattr(cs.Sandbox, "load_deps", boom)
    assert cs.refresh_images(make_args(repo="o/r")) == 1


def test_the_refresher_needs_no_repository():
    p = cs.build_parser()
    a = p.parse_args(["--refresh-images"])
    cs.normalize_args(a)            # must not raise "a repository is required"
    assert a.refresh_images and a.hard_max_age == 30
