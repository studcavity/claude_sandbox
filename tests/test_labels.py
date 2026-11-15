import pytest

from claude_sandbox.github import labels as sl
@pytest.mark.parametrize("s,expect", [
    ("frosty-volta", "session:frosty-volta"), ("a", "session:a"),
    ("", None), (None, None), ("-bad", None), ("has space", None),
    ("x" * 41, None), ("evil\nname", None),
])
def test_session_label_only_for_valid_session_names(s, expect):
    assert sl.session_label(s) == expect


def test_every_named_label_has_a_colour_and_description():
    for name in (sl.SANDBOX, sl.FOLLOW_UP):
        color, desc = sl.colour_and_description(name)
        assert len(color) == 6 and desc
    assert sl.colour_and_description("session:x")[0] == "ededed"


def test_ensure_creates_each_label_once_and_swallows_refusals():
    calls = []

    def api(method, path, body=None):
        calls.append((method, path, body["name"]))
        if body["name"] == sl.FOLLOW_UP:
            raise RuntimeError("422 already exists")
        return {}
    done = sl.ensure(api, "o/r", [sl.SANDBOX, sl.FOLLOW_UP, sl.SANDBOX])
    assert [c[2] for c in calls] == [sl.SANDBOX, sl.FOLLOW_UP]   # no repeat
    assert done == {sl.SANDBOX, sl.FOLLOW_UP}
    sl.ensure(api, "o/r", [sl.SANDBOX, sl.FOLLOW_UP], done)       # shared set: nothing new
    assert len(calls) == 2
    assert all(c[:2] == ("POST", "/repos/o/r/labels") for c in calls)
