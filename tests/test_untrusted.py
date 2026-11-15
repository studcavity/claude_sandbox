import itertools

import pytest

import sandbox_actions as sa
import sandbox_land as sl
import sandbox_untrusted as su


# --- parity: the refactor must not change a single output ------------------ #
# Verbatim copies of the implementations this module replaced.
def old_clean_block(text, limit):
    t = "".join(c if c.isprintable() or c == "\n" else " "
                for c in text.replace("\r", ""))
    t = t.replace("@", "@​").strip()
    return t if len(t) <= limit else t[:limit - 1] + "…"


def old_clean_text(text, limit):
    t = "".join(c if c.isprintable() else " " for c in text)
    t = " ".join(t.split()).replace("`", "'").replace("@", "@​")
    return t if len(t) <= limit else t[:limit - 1] + "…"


HOSTILE = [
    "", "plain", "hi @octocat and @org/team", "a\r\nb\rc\n\n\nd", "\x1b[31mred\x00",
    "```fence``` `x`", "tab\there nbsp", "emoji 🙂 and ​ zwsp",
    "  padded  ", "@" * 5, "x" * 500, "line1\nline2 @bob `c`\n" * 40,
    "﻿bom", " sep ", "mixed\x07bell\x7fdel",
]


@pytest.mark.parametrize("text,limit", list(itertools.product(HOSTILE, (1, 7, 80, 1000))))
def test_cleaners_match_the_implementations_they_replaced(text, limit):
    assert sa.clean_block(text, limit) == old_clean_block(text, limit)
    assert sl.clean_text(text, limit) == old_clean_text(text, limit)


def test_clean_line_still_builds_on_clean_block():
    assert sa.clean_line("a\nb @x\x00", 50) == " ".join(
        old_clean_block("a\nb @x\x00", 100).split())[:50]


# --- secret scan ----------------------------------------------------------- #
# Fixtures are assembled at runtime so no token-shaped literal sits in the
# source: the checker scans every commit's added lines, and GitHub's push
# protection looks for the same shapes.
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2          # 44 chars after the prefix
POSITIVE = {
    "GitHub token": [GH, f"use {GH} to push", "gho_" + "x9Y8" * 10,
                     "ghs_" + "Q" * 36],
    "GitHub fine-grained token": ["github_pat_" + "11AB" * 6 + "_x"],
    "Anthropic key": ["sk-ant-api03-" + "Zz9-_" * 6, "sk-ant-oat01-" + "abcdefghijklmnopqrstu"],
    "AWS access key id": ["AKIA" + "IOSFODNN7EXAMPLE", "ASIA" + "IOSFODNN7EXAMPLE"],
    "Slack token": ["xoxb-" + "1234567890-abcdefghij"],
    "private key": ["-----BEGIN RSA " + "PRIVATE KEY-----", "-----BEGIN " + "PRIVATE KEY-----",
                    "-----BEGIN OPENSSH " + "PRIVATE KEY-----"],
    "credential assignment": ["api_key = " + "8f3kQ9zL2mX7vB1nR5tY0wA4cE6", 'token: "' + "Zx9Qm2Lp8Rt4Vb7Nc1Ke5Ws3Dh" + '"',
                              "MY_SECRET=" + "aB3dE5gH7jK9mN1pQ3sT5vX7zC9"],
}


@pytest.mark.parametrize("label,text", [(l, t) for l, ts in POSITIVE.items() for t in ts])
def test_detects_each_kind(label, text):
    assert label in su.find_secrets(text)


@pytest.mark.parametrize("text", [
    "", "plain prose about tokens and secrets", "ghp_short", "ghp_" + "a" * 10,
    "sk-ant", "AKIA123", "token: abc", "password: hunter2", "secret = changeme",
    "api_key = aaaaaaaaaaaaaaaaaaaaaaaaaaaa",           # low entropy
    "token: " + "a1" * 12,                                # low entropy, 24 chars
    "see commit 0123456789abcdef0123456789abcdef01234567",  # a sha, no keyword
    "BEGIN PRIVATE KEY without dashes", "mask the token before logging",
    "path /usr/local/lib/python3/site-packages/x.py",
])
def test_near_misses_are_not_flagged(text):
    assert su.find_secrets(text) == []


def test_findings_never_contain_the_secret():
    labels = su.find_secrets(f"oops {GH} and " + "AKIA" + "IOSFODNN7EXAMPLE")
    assert labels == ["AWS access key id", "GitHub token"]
    assert all(GH not in l and "AKIA" not in l for l in labels)


def test_entropy_orders_obvious_cases():
    assert su.entropy("") == 0 and su.entropy("aaaa") == 0
    assert su.entropy("aB3dE5gH7jK9mN1p" + "Q3sT5vX7zC9") > su.MIN_ENTROPY > su.entropy("a1" * 12)


# --- wired into issue filing ---------------------------------------------- #
from test_actions import A, Api, ctx, issue, msg  # noqa: E402


def test_issue_with_a_secret_is_held_back_and_reported_not_filed():
    api = Api()
    acts = sa.parse_commits([(A, msg(
        issue("leak", "Rotate it", body=f"found {GH} in the log"),
        issue("fine", "Ordinary follow-up")))])
    out = sa.execute(ctx(api), acts)
    by_id = {o.id: o for o in out}
    assert by_id["leak"].status == "rejected"
    assert "GitHub token" in by_id["leak"].detail and GH not in by_id["leak"].detail
    assert by_id["fine"].status == "done"            # one bad item never blocks others
    assert len(api.posts("/issues")) == 1            # only the clean one was filed
    assert not any(GH in str(c) for c in api.calls)  # the secret was never sent anywhere
    assert len(api.posts("/comments")) == 1          # the rejected item isn't marked done


def test_secret_in_the_title_is_caught_too():
    api = Api()
    acts = sa.parse_commits([(A, msg(issue("t", f"key {GH}")))])
    assert sa.execute(ctx(api), acts)[0].status == "rejected"
    assert api.posts("/issues") == []


def test_rejection_is_visible_in_the_exit_summary():
    text = sa.format_outcomes([sa.Outcome(A, "leak", "issue", "rejected",
                                          "not filed: the text appears to contain GitHub token")])
    assert "REJECTED (policy)" in text and "GitHub token" in text
    assert A[:7] in text


def test_malformed_payloads_stay_silent_unlike_policy_rejections():
    # the spec: nonconforming = silent no-op; only policy rejections surface
    api = Api()
    acts = sa.parse_commits([(A, msg(issue("e", "   ")))])
    assert sa.execute(ctx(api), acts) == []
