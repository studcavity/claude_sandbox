"""sandbox_untrusted.py - handling of text written by an AI session.

Commit messages, branch names and `todo/` briefs come from a sandboxed agent
and end up pasted into GitHub issues and PRs under the *user's* identity. Two
modules used to carry their own near-copies of the cleaning code
(`sandbox_land.clean_text`, `sandbox_actions.clean_block`); both now build on
the primitives here so a fix lands once. Stdlib only (the checker imports it).

Two jobs:
  * make text safe to paste: no control characters, no live @-mentions or
    issue references (`#12`, `owner/repo#12`, `GH-12`), bounded;
  * refuse to publish text that looks like it contains a credential.

The secret scan is a tripwire for accidents (a token pasted into a commit
message, a log line in a brief), not a defence against an adversary who can
encode a secret. It reports WHAT KIND of secret matched and never the match
itself, so the finding can be logged without leaking it a second time.
"""
import math
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
# (label, pattern). Prefix-anchored token formats are precise; the generic
# `name = value` rule below is entropy-gated to keep false positives down.
TOKEN_PATTERNS = (
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token",
     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
)
# `token: abc...`, `API_KEY="abc..."`, `password = abc...` (value >= 24 chars).
ASSIGNMENT = re.compile(
    r"(?i)\b(?:[a-z0-9_-]*(?:token|secret|passwd|password|api[_-]?key))\b"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-+/=.]{24,})")
MIN_ENTROPY = 3.5  # bits per character; English words and paths sit below


def entropy(s):
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def find_secrets(text):
    """Labels of the credential kinds `text` appears to contain (sorted, no
    duplicates, never the matched text)."""
    found = {label for label, rx in TOKEN_PATTERNS if rx.search(text)}
    for m in ASSIGNMENT.finditer(text):
        if entropy(m.group(1)) >= MIN_ENTROPY:
            found.add("credential assignment")
    return sorted(found)
