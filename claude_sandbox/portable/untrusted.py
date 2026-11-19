"""portable/untrusted.py - handling of text written by an AI session.

Commit messages, branch names and `todo/` briefs come from a sandboxed agent
and end up pasted into GitHub issues and PRs under the *user's* identity. Two
modules used to carry their own near-copies of the cleaning code
(`land.clean_text`, `issues.clean_block`); both now build on
the primitives here so a fix lands once. Stdlib only (the checker imports it).

Two jobs:
  * make text safe to paste: no control characters, no live @-mentions or
    issue references (`#12`, `owner/repo#12`, `GH-12`), bounded;
  * refuse to publish text that looks like it contains a credential.

The secret scan is a tripwire for accidents (a token pasted into a commit
message, a log line in a brief), not a defence against an adversary who can
encode a secret. It reports WHAT KIND of secret matched and never the match
itself, so the finding can be logged without leaking it a second time. There
is no way to mark a match as allowed: a fixture is assembled at runtime
(`"ghp_" + "x" * 40`) so nothing token-shaped is in the source.

Where it runs, and why each place needs it:
  * `check` on commit messages and on every line a commit adds: the
    one scan on the way in, before anything is merged;
  * `land.safe_line` on PR titles and bodies: the text is copied from
    commits, but a PR body is mailed to everyone watching the repo, which a
    pushed commit is not;
  * `issues` on a `todo/` file before it is published as an issue (a
    repo without the checker workflow never scanned it);
  * `provenance.override_description` on the reason a person types,
    which becomes a public status description.
GitHub push protection (docs/security/secrets-scanning.md) is the preferred first line where it exists.
"""
import re

ZWSP = "​"


def printable(text, keep_newlines=False):
    """Control characters (and, unless kept, newlines) become spaces."""
    return "".join(c if c.isprintable() or (keep_newlines and c == "\n")
                   else " " for c in text)


def defang_mentions(text):
    """`@user` would notify someone; a zero-width space after the @ stops it."""
    return text.replace("@", "@" + ZWSP)


# `#12`, `owner/repo#12` and `GH-12` autolink. In a PR or issue body written
# under the user's identity that means a cross-reference event on someone
# else's issue (other repos included), and in a PR body "closes #12" closes
# #12 when the PR merges. A zero-width space before the number stops both.
_REFERENCE = re.compile(r"(#|\bGH-)(?=\d)", re.IGNORECASE)


def defang_references(text):
    return _REFERENCE.sub(lambda m: m.group(1) + ZWSP, text)


def defang(text):
    """Everything that would make GitHub act on the text: mentions, refs."""
    return defang_references(defang_mentions(text))


def truncate(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def block(text, limit):
    """Multi-line text safe to paste: newlines kept, `\\r` dropped."""
    t = defang(printable(text.replace("\r", ""), keep_newlines=True))
    return truncate(t.strip(), limit)


def one_line(text, limit, backticks_to_quotes=False):
    """Single-line text safe to paste. `backticks_to_quotes` for text that is
    placed inside a code span or fence."""
    t = " ".join(printable(text).split())
    if backticks_to_quotes:
        t = t.replace("`", "'")
    return truncate(defang(t), limit)


# --- secret scan ----------------------------------------------------------- #
# (label, pattern): prefix-anchored token formats, each aimed at a credential
# the sandbox or its user actually holds. A session can only leak what it can
# see, and the launcher puts exactly three secrets in a container: the Claude
# credential (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY, both `sk-ant-`),
# the deploy key and the session signing key (OpenSSH private keys). The
# GitHub formats are for the user's own `gh` credentials, which sit one
# careless paste away from a session's reach and are what GitHub's own push
# protection looks for. Credentials the sandbox never holds (AWS, Slack, ...)
# and generic `password = <high-entropy>` assignments are NOT scanned: they
# were the false-positive source, and GitHub's secret scanning covers them.
TOKEN_PATTERNS = (
    # the Claude credential the launcher passes in
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    # the deploy key and the session signing key (any PEM/OpenSSH private key)
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    # the user's GitHub credentials (classic, OAuth, app, and fine-grained)
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token",
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
)


def find_secrets(text):
    """Labels of the credential kinds `text` appears to contain (sorted, no
    duplicates, never the matched text)."""
    return sorted(label for label, rx in TOKEN_PATTERNS if rx.search(text))
