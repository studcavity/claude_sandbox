import pytest

from claude_sandbox.github import issues as si
from claude_sandbox.github import land as sl
from claude_sandbox.github import templates as st
def tdir(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return str(tmp_path)


GOOD_PR = "$session $branch $caveats $subjects $more untrusted"
GOOD_ISSUE = "$commit $item $body untrusted"


def test_the_shipped_templates_load():
    for name in st.REQUIRED:
        assert "untrusted" in st.load(name).lower()


@pytest.mark.parametrize("text,fragment", [
    ("$session $branch $subjects $more untrusted", "missing $caveats"),
    ("$session $branch $caveats $subjects $more $extra untrusted", "unknown $extra"),
    ("$session $branch $caveats $subjects $more", "untrusted"),
    ("$session $branch $caveats $subjects $more untrusted costs $5 now", "lone `$`"),
])
def test_a_template_that_drops_or_invents_something_is_refused(tmp_path, text, fragment):
    with pytest.raises(st.TemplateError, match="lone|" + fragment.replace("$", r"\$")):
        st.load("pr.md", tdir(tmp_path, **{"pr.md": text}))


def test_unknown_names_and_missing_files_are_refused(tmp_path):
    with pytest.raises(st.TemplateError, match="unknown template"):
        st.load("evil.md")
    with pytest.raises(st.TemplateError, match="pr.md"):
        st.load("pr.md", str(tmp_path))                       # no such file there


def test_braced_placeholders_and_escaped_dollars_work(tmp_path):
    d = tdir(tmp_path, **{"issue.md": "${commit}x $item $body untrusted $$5\n"})
    assert st.render("issue.md", d, commit="c", item="i", body="b") \
        == "cx i b untrusted $5"


def test_values_are_not_expanded_again(tmp_path):
    d = tdir(tmp_path, **{"issue.md": GOOD_ISSUE})
    out = st.render("issue.md", d, commit="$item", item="$$", body="${item}")
    assert out.startswith("$item $$ ${item} untrusted")


def test_a_missing_value_fails_loudly(tmp_path):
    with pytest.raises(KeyError):
        st.render("issue.md", tdir(tmp_path, **{"issue.md": GOOD_ISSUE}), commit="c")


def test_one_trailing_newline_is_dropped_but_not_more(tmp_path):
    d = tdir(tmp_path, **{"pr.md": GOOD_PR + "\n\n"})
    assert st.load("pr.md", d).endswith("untrusted\n")


# --- golden parity with the code the templates replaced ------------------------- #
def old_issue(sha, item, body, marker):
    return (f"Follow-up requested by commit {sha}, item `{item}`.\n\n{body}"
            "\n\n---\nFiled by claude_sandbox from a `todo/` file that "
            "reached the default branch. The text above was written by an AI "
            "session and is untrusted.\n" + marker)


def test_issue_body_is_exactly_the_documented_text():
    sha, marker = "a" * 40, f"<!-- {si.MARKER} sha={'a' * 40} id=i1 -->"
    got = st.render("issue.md", commit=sha, item="i1",
                    body="the body") + "\n" + marker
    assert got == old_issue(sha, "i1", "the body", marker)


def old_pr(session, branch, shown, caveats_text, more):
    return (f"Opened by claude_sandbox when session `{session}` ended, from "
            f"branch `{branch}`.\n\n"
            "Everything below was written by an AI session and is untrusted: "
            "review the diff, not this text.\n\n"
            + caveats_text + f"````\n{shown}\n````\n" + more)


@pytest.mark.parametrize("n_subjects", [1, 3, 25])
@pytest.mark.parametrize("with_caveats", [False, True])
def test_pr_body_is_byte_identical_to_the_old_inline_text(n_subjects, with_caveats):
    subjects = [f"feat: change {i}" for i in range(n_subjects)]
    items = sl.not_verified_lines([("aaaaaaa", "feat: x\n\nNot verified: pytest")]) if with_caveats else None
    _, body = sl.pr_text("sess", "sbx/sess/work", subjects, items)
    shown = "\n".join(sl.safe_line(s, 120) for s in subjects[:sl.MAX_SUBJECTS])
    more = n_subjects - sl.MAX_SUBJECTS
    assert body == old_pr(
        "sess", "sbx/sess/work", shown,
        (sl.caveats_section(items) + "\n") if items is not None else "",
        f"\n...and {more} more commits.\n" if more > 0 else "")


def test_the_marker_is_always_the_last_line_whatever_the_template_says():
    from test_issues import A, Api, req
    api = Api()
    si.execute(api, "o/r", [req("i1")])
    body = api.issues[0]["body"]
    assert body.splitlines()[-1] == f"<!-- {si.MARKER} sha={A} id=i1 -->"
