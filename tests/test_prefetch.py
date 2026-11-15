"""The module is named test_prefetch on purpose: conftest leaves the prefetch
switched on only here, against a faked `_gh` (never a real gh)."""
import json
import threading
import types

import pytest

import sandbox_github as g


class Gh:
    """Fake `_gh`: records calls, can block on a barrier or fail for a path."""

    def __init__(self, barrier=None, fail=()):
        self.calls, self.barrier, self.fail = [], barrier, set(fail)
        self.lock = threading.Lock()

    def __call__(self, args, body=None):
        with self.lock:
            self.calls.append(list(args))
        if self.barrier:
            self.barrier.wait(timeout=5)       # only releases if all are in flight
        path = args[-1] if args[0] == "api" else ""
        if any(f in " ".join(args) for f in self.fail):
            return types.SimpleNamespace(failed=True, exited=1,
                                         stdout="", stderr="gh: Not Found (HTTP 404)")
        if args[:2] == ["api", "user"]:
            return types.SimpleNamespace(failed=False, stdout="studcavity\n", stderr="")
        if args[:3] == ["api", "-i", "/"]:
            return types.SimpleNamespace(
                failed=False, stderr="",
                stdout="HTTP/2 200\r\nX-OAuth-Scopes: repo, workflow\r\n\r\n{}")
        return types.SimpleNamespace(failed=False, stderr="",
                                     stdout=json.dumps({"path": path}))


@pytest.fixture(autouse=True)
def clean():
    g.clear_prefetch()
    yield
    g.clear_prefetch()


@pytest.fixture
def gh(monkeypatch):
    fake = Gh()
    monkeypatch.setattr(g, "_gh", fake)
    return fake


API = ("api", "/repos/o/r", False)


def test_a_prefetched_get_is_answered_once_without_a_second_call(gh):
    g.start_prefetch([API])
    assert g.gh_api("GET", "/repos/o/r") == {"path": "/repos/o/r"}
    assert len([c for c in gh.calls if c[-1] == "/repos/o/r"]) == 1
    g.gh_api("GET", "/repos/o/r")                           # consumed: live again
    assert len([c for c in gh.calls if c[-1] == "/repos/o/r"]) == 2


def test_only_an_exact_match_consumes_it(gh):
    g.start_prefetch([API])
    g.gh_api("GET", "/repos/o/r", raw=True)                  # raw differs: live
    g.gh_api("GET", "/repos/o/other")                        # other path: live
    g.gh_api("POST", "/repos/o/r", {"x": 1})                 # never a write: live
    g.gh_api("GET", "/repos/o/r", {"x": 1})                  # nor a GET with a body: live
    before = len(gh.calls)
    assert g.gh_api("GET", "/repos/o/r") == {"path": "/repos/o/r"}   # the prefetch, intact
    assert len(gh.calls) == before                           # answered without a new call
    methods = sorted(c[2] for c in gh.calls if c[3] == "/repos/o/r")
    assert methods == ["GET", "GET", "GET", "POST"]          # prefetch + raw + body-GET + POST


def test_login_and_scopes_are_prefetched_too(gh):
    g.start_prefetch([("login",), ("scopes",)])
    assert g.github_login() == "studcavity"
    assert g.token_scopes() == {"repo", "workflow"}
    n = len(gh.calls)
    assert g.github_login() == "studcavity"                  # live the second time
    assert len(gh.calls) == n + 1


def test_the_reads_really_run_concurrently(monkeypatch):
    barrier = threading.Barrier(3)                           # needs 3 calls in flight at once
    monkeypatch.setattr(g, "_gh", Gh(barrier))
    g.start_prefetch([("login",), ("scopes",), ("api", "/repos/o/r", False)])
    assert g.github_login() == "studcavity"                  # a serial run would time out
    assert g.token_scopes() == {"repo", "workflow"}
    assert g.gh_api("GET", "/repos/o/r")["path"] == "/repos/o/r"


def test_a_failure_replays_where_the_value_is_used_not_at_start(monkeypatch):
    monkeypatch.setattr(g, "_gh", Gh(fail=["contents/x.yaml"]))
    g.start_prefetch([("api", "/repos/o/r/contents/x.yaml", True)])   # no exception here
    with pytest.raises(g.GitHubError) as e:
        g.gh_api("GET", "/repos/o/r/contents/x.yaml", raw=True)
    assert e.value.code == 404                               # same as a direct call


def test_entries_expire_so_a_late_read_is_fresh(gh, monkeypatch):
    monkeypatch.setattr(g, "PREFETCH_TTL", -1.0)
    g.start_prefetch([API])
    g.gh_api("GET", "/repos/o/r")
    assert len([c for c in gh.calls if c[-1] == "/repos/o/r"]) == 2   # prefetch wasted, live used


def test_disabled_means_no_calls_at_all(gh, monkeypatch):
    monkeypatch.setattr(g, "PREFETCH_ENABLED", False)
    g.start_prefetch([API, ("login",)])
    assert gh.calls == [] and g._prefetched == {}


def test_clear_forgets_unconsumed_results(gh):
    g.start_prefetch([API])
    g.clear_prefetch()
    n = len(gh.calls)
    g.gh_api("GET", "/repos/o/r")
    assert len(gh.calls) == n + 1


def test_unknown_specs_are_a_programming_error(gh):
    with pytest.raises(ValueError):
        g.start_prefetch([("nope",)])


# --- the plan ------------------------------------------------------------------- #
def args(**kw):
    base = dict(repo="o/r", setup_token=False, land_all=False, no_prefetch=False,
                no_deps=False, no_usb=False, read_only=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


def plan(**kw):
    import claude_sandbox as cs
    return cs.prefetch_plan(args(**kw))


def test_full_plan_covers_the_independent_reads_the_launch_makes():
    specs = plan()
    assert ("login",) in specs and ("scopes",) in specs
    paths = [s[1] for s in specs if s[0] == "api"]
    assert paths == ["/repos/o/r/contents/claude_sandbox_deps.yaml",
                     "/repos/o/r/contents/claude_sandbox_host.yaml",
                     "/repos/o/r", "/repos/o/r/keys?per_page=100"]
    assert all(s[2] is True for s in specs if s[0] == "api" and "contents" in s[1])


@pytest.mark.parametrize("kw,missing", [
    (dict(no_deps=True), "claude_sandbox_deps.yaml"),
    (dict(no_usb=True), "claude_sandbox_host.yaml"),
    (dict(read_only=True), "keys?per_page"),
])
def test_flags_remove_what_the_launch_will_not_ask_for(kw, missing):
    assert not any(missing in str(s) for s in plan(**kw))
    if "read_only" in kw:
        assert ("scopes",) not in plan(**kw)


@pytest.mark.parametrize("kw", [dict(repo=None), dict(setup_token=True),
                                dict(land_all=True), dict(no_prefetch=True)])
def test_nothing_is_started_where_it_would_be_wasted_or_unwanted(kw):
    assert plan(**kw) == []
