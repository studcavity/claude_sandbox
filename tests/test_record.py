import pytest

import sandbox_land as sl
import sandbox_record as sr

GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2    # assembled: nothing token-shaped in source
A = "a" * 40
ACTION = ('Sandbox-Action: {"v": 1, "type": "issue", "id": "i1", '
          '"title": "Run the smoke test"}')


def rec(*pairs):
    return sr.extract(list(pairs))


def test_extracts_each_kind_with_the_commit_it_came_from():
    r = rec(("aaaaaaa", "feat: x\n\nVerified: pyflakes, pytest\nNot verified against real incus\n"
                        "Follow-up: run ./smoke.sh on a host\n\n" + ACTION),
            ("bbbbbbb", "fix: y\n\nVerified inside a sandbox without nesting"))
    assert r.verified == [("aaaaaaa", "Verified: pyflakes, pytest"),
                          ("bbbbbbb", "Verified inside a sandbox without nesting")]
    assert r.not_verified == [("aaaaaaa", "Not verified against real incus")]
    assert r.follow_ups == [("aaaaaaa", "run ./smoke.sh on a host")]
    assert r.issues == [("aaaaaaa", "i1", "Run the smoke test")]


@pytest.mark.parametrize("line", [
    "we never verified this", "unverified", "Verifiedish", "see: Verified: x",
    "Follow up without colon", "followup: no hyphen",
])
def test_only_lines_that_start_with_the_words_count(line):
    r = rec(("aaaaaaa", f"feat: x\n\n{line}"))
    assert not r.verified and not r.not_verified


def test_not_verified_is_never_counted_as_verified():
    r = rec(("aaaaaaa", "feat: x\n\nNot verified: no incus"))
    assert r.not_verified and not r.verified


def test_malformed_or_non_issue_trailers_are_ignored():
    r = rec(("aaaaaaa", 'feat: x\n\nSandbox-Action: {"v": 1, "type": "resolve", "id": "r", "ref": "a:b"}'),
            ("bbbbbbb", "feat: x\n\nSandbox-Action: not json"))
    assert r.issues == []


def test_bounds_hold_on_hostile_input():
    # only the first MAX_LINES_PER_MESSAGE lines of a message are read
    late = "\n".join(["filler"] * sr.MAX_LINES_PER_MESSAGE + ["Verified: too late"])
    assert rec(("aaaaaaa", late)).verified == []
    # and each kind keeps at most MAX_KEPT items
    r = rec(("aaaaaaa", "\n".join(["Verified: x"] * 5000)))
    assert len(r.verified) == sr.MAX_KEPT
    many = [(f"{i:07x}", "Follow-up: item") for i in range(sr.MAX_KEPT + 50)]
    assert len(sr.extract(many).follow_ups) == sr.MAX_KEPT


def test_is_empty():
    assert sr.is_empty(sr.EMPTY) and sr.is_empty(rec(("aaaaaaa", "feat: x")))
    assert not sr.is_empty(rec(("aaaaaaa", "Verified: x")))


# --- the PR body ---------------------------------------------------------------- #
def body(record, subjects=("feat: one",)):
    return sl.pr_text("sess", "sbx/sess/work", list(subjects), record)[1]


def test_body_lists_what_the_commits_say_before_the_subjects():
    r = rec(("aaaaaaa", "feat: x\n\nVerified: pytest\nNot verified: no incus\n"
                        "Follow-up: do y\n\n" + ACTION))
    b = body(r)
    for needle in ("### What the commits say", "**Verified** (1)", "`aaaaaaa` Verified: pytest",
                   "**Not verified** (1)", "**Follow-ups** (1)", "`aaaaaaa` do y",
                   "**Issues the launcher will file** (1)", "Run the smoke test", "(`i1`)"):
        assert needle in b
    assert b.index("What the commits say") < b.index("````")     # before the fenced subjects
    assert "untrusted" in b


def test_body_says_so_when_no_commit_mentions_verification():
    assert "No `Verified` / `Not verified` statement in any commit" in body(rec(("aaaaaaa", "feat: x")))
    assert "No `Verified`" not in body(rec(("aaaaaaa", "feat: x\n\nNot verified: x")))


def test_long_lists_are_truncated_with_a_count():
    r = rec(*[(f"{i:07x}", f"feat: {i}\n\nFollow-up: item {i}") for i in range(15)])
    b = body(r)
    assert "**Follow-ups** (15)" in b and "...and 5 more" in b
    assert "item 9" in b and "item 10" not in b


def test_session_text_is_sanitised_in_the_record():
    r = rec(("aaaaaaa", "feat: x\n\nFollow-up: ping @octocat `rm -rf` \x1b[31m"))
    b = body(r)
    assert "@octocat" not in b and "`rm -rf`" not in b and "\x1b" not in b


def test_a_credential_in_a_commit_message_is_withheld_not_copied_into_the_pr():
    r = rec(("aaaaaaa", f"feat: x\n\nVerified: used {GH} to push\nFollow-up: rotate {GH}"))
    b = body(r, subjects=[f"feat: add {GH}"])
    assert GH not in b
    assert b.count("[withheld: appears to contain GitHub token]") == 3   # verified, follow-up, subject


def test_a_forged_sha_cannot_break_out_of_its_code_span():
    r = sr.Record([("a`b`<script>", "Verified: x")], [], [], [])
    assert "<script>" not in body(r) and "`?`" in body(r)


def test_without_a_record_the_body_is_unchanged():
    t, b = sl.pr_text("sess", "sbx/sess/work", ["feat: one"])
    assert "What the commits say" not in b and t == "[sess] feat: one"


# --- proposed synopsis ------------------------------------------------------------ #
SYN = "feat: x\n\nPR-Title: Add the thing\nPR-Summary: does a\nPR-Summary: because b\n"


def test_synopsis_comes_from_the_newest_commit_that_has_a_title():
    r = rec(("aaaaaaa", SYN), ("bbbbbbb", "PR-Title: Better title\nPR-Summary: new"),
            ("ccccccc", "fix: z\n\nPR-Summary: orphan summary, no title"))
    assert r.synopsis == ("bbbbbbb", "Better title", ["new"])
    assert rec(("aaaaaaa", "feat: x")).synopsis is None


def test_body_shows_the_synopsis_as_untrusted_and_sanitised():
    r = rec(("aaaaaaa", "feat: x\n\nPR-Title: ping @octocat `rm` fixes #12\n"
                        f"PR-Summary: leaked {GH}\nPR-Summary: ok\x1b[31m"))
    b = body(r)
    assert "### Proposed synopsis" in b and "untrusted" in b
    assert b.index("Proposed synopsis") < b.index("What the commits say")
    assert "@octocat" not in b and "`rm`" not in b and "#12" not in b.replace("#​12", "")
    assert GH not in b and "[withheld: appears to contain GitHub token]" in b
    assert "\x1b" not in b
    t, _ = sl.pr_text("sess", "sbx/sess/work", ["feat: one"], r)
    assert t.endswith(" [sess]") and "@octocat" not in t and "`" not in t
    assert t.startswith("ping @\u200boctocat")


def test_no_synopsis_section_without_a_synopsis():
    assert "Proposed synopsis" not in body(rec(("aaaaaaa", "feat: x")))


def test_title_is_the_proposed_one_with_the_session_appended():
    t, _ = sl.pr_text("sess", "sbx/sess/work", ["feat: one", "feat: two"], rec(("aaaaaaa", SYN)))
    assert t == "Add the thing [sess]"
    t, _ = sl.pr_text("sess", "sbx/sess/work", ["feat: one"], rec(("aaaaaaa", "feat: x")))
    assert t == "[sess] feat: one"          # no proposal: built from the subjects
