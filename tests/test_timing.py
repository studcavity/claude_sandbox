import json

import pytest

import sandbox_timing as st
import sandbox_util


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def tick(self, s):
        self.now += s


@pytest.fixture(autouse=True)
def uninstall():
    yield
    st.install(None)


def test_stages_nest_and_report_exact_numbers():
    c = Clock()
    t = st.Timings(c)
    with t.stage("outer"):
        c.tick(1)
        with t.stage("inner"):
            c.tick(2)
        c.tick(1)
    c.tick(4)
    d = t.to_dict()
    assert d["total_seconds"] == 8.0
    assert [(r["name"], r["depth"], r["start"], r["seconds"]) for r in d["stages"]] == [
        ("outer", 0, 0.0, 4.0), ("inner", 1, 1.0, 2.0)]
    text = t.report()
    assert "outer" in text and "50%" in text          # 4 of 8 seconds
    assert "  inner" in text                           # indented under its parent


def test_a_stage_is_recorded_even_when_it_raises():
    c = Clock()
    t = st.Timings(c)
    with pytest.raises(RuntimeError):
        with t.stage("boom"):
            c.tick(3)
            raise RuntimeError("x")
    assert t.to_dict()["stages"][0]["seconds"] == 3.0
    with t.stage("after"):                              # depth bookkeeping survived
        pass
    assert [r["depth"] for r in t.to_dict()["stages"]] == [0, 0]


def test_prompt_mark_is_the_headline_and_first_mark_wins():
    c = Clock()
    t = st.Timings(c)
    c.tick(5)
    t.mark("prompt")
    c.tick(5)
    t.mark("prompt")
    assert t.to_dict()["marks"] == {"prompt": 5.0}
    assert "the session prompt appeared after 5.00 s (50% of 10.00 s total)" in t.report()


def test_counters_sum_calls_and_seconds_biggest_first():
    t = st.Timings(Clock())
    t.count("gh", 0.5)
    t.count("gh", 0.25)
    t.count("incus exec", 2.0)
    d = t.to_dict()["counters"]
    assert list(d) == ["incus exec", "gh"]
    assert d["gh"] == {"calls": 2, "seconds": 0.75}


def test_json_roundtrip(tmp_path):
    t = st.Timings(Clock())
    with t.stage("a"):
        pass
    p = tmp_path / "t.json"
    st.write_json(t, str(p))
    assert json.loads(p.read_text())["version"] == 1


def test_helpers_are_noops_when_nothing_is_installed():
    assert st.ACTIVE is None
    with st.stage("x"):
        pass
    st.mark("prompt")
    st.count("gh", 1.0)           # nothing to assert on: it must simply not raise


def test_helpers_feed_the_installed_timings():
    t = st.Timings(Clock())
    st.install(t)
    with st.stage("x"):
        pass
    st.mark("m")
    st.count("gh", 1.0)
    d = t.to_dict()
    assert d["stages"][0]["name"] == "x" and "m" in d["marks"] and "gh" in d["counters"]


@pytest.mark.parametrize("cmd,label", [
    (["incus", "exec", "c", "--", "x"], "incus exec"),
    (["/usr/bin/incus", "launch", "img"], "incus launch"),
    (["lxc", "config", "set"], "lxc config"),
    (["git", "clone", "u"], "git"), (["/usr/bin/gh", "api"], "gh"), ([], "?"),
])
def test_label_for(cmd, label):
    assert st.label_for(cmd) == label


def test_run_counts_subprocesses_only_when_timing_is_on(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox_util.subprocess, "run", lambda *a, **k: calls.append(a) or "r")
    clock_reads = []
    monkeypatch.setattr(sandbox_util.time, "perf_counter",
                        lambda: clock_reads.append(1) or 0.0)
    assert sandbox_util.run(["incus", "exec", "c"]) == "r"
    assert clock_reads == []                       # off: the clock isn't even read
    t = st.Timings(Clock())
    st.install(t)
    sandbox_util.run(["incus", "exec", "c"])
    assert t.to_dict()["counters"]["incus exec"]["calls"] == 1 and clock_reads


def test_a_failing_subprocess_is_still_counted(monkeypatch):
    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(sandbox_util.subprocess, "run", boom)
    t = st.Timings(Clock())
    st.install(t)
    with pytest.raises(OSError):
        sandbox_util.run(["git", "x"])
    assert t.to_dict()["counters"]["git"]["calls"] == 1


def test_gh_calls_are_counted(monkeypatch):
    import sandbox_github

    class Ctx:
        def run(self, *a, **k):
            return type("R", (), {"failed": False, "stdout": "{}"})()
    monkeypatch.setattr(sandbox_github, "Context", Ctx)
    t = st.Timings(Clock())
    st.install(t)
    sandbox_github._gh(["api", "user"])
    assert t.to_dict()["counters"]["gh"]["calls"] == 1
