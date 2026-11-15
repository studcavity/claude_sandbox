"""The follow-up request format (`todo/<name>.md`), pure rules."""
import pytest

import sandbox_todo as st


@pytest.mark.parametrize("path,name", [
    ("todo/fix-ci.md", "fix-ci"), ("todo/a.md", "a"), ("todo/0-x.md", "0-x"),
    ("todo/" + "a" * 80 + ".md", "a" * 80),
    ("todo/Fix.md", None), ("todo/-x.md", None), ("todo/a_b.md", None),
    ("todo/a.txt", None), ("todo/sub/a.md", None), ("src/todo/a.md", None),
    ("todo/" + "a" * 81 + ".md", None), ("todo/.md", None), ("todo/a b.md", None),
])
def test_request_names(path, name):
    assert st.request_name(path) == name


def test_candidates_are_markdown_files_under_todo_whatever_their_name():
    assert st.is_candidate("todo/Bad_Name.MD") and st.is_candidate("todo/a/b.md")
    assert not st.is_candidate("todo/x.txt") and not st.is_candidate("docs/todo/x.md")


@pytest.mark.parametrize("text,expect", [
    ("# Title\nbody", "Title"), ("\n\nintro\n## Second ##  \n", "Second"),
    ("###### deep", "deep"), ("#nospace\nbody", None), ("", None),
    ("no heading", None), ("#   \n", None),
])
def test_title_is_the_first_heading(text, expect):
    assert st.title(text) == expect


def test_problems():
    assert st.problems("todo/ok.md", 10, "# T\n") == []
    assert "no heading" in st.problems("todo/ok.md", 10, "text")[0]
    assert "todo/<name>.md" in st.problems("todo/Bad.md", 10, "# T")[0]
    big = st.problems("todo/ok.md", st.MAX_BYTES + 1)
    assert len(big) == 1 and "limit" in big[0]      # text isn't needed, or read
    assert st.problems("todo/ok.md", st.MAX_BYTES, "# T") == []
