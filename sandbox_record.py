"""sandbox_record.py - what a branch's commit messages say about themselves.

The repo asks sessions to state, in each commit message, what was and wasn't
verified and what is left to do (`Verified`, `Not verified` and `Follow-up:`
lines). A reviewer should not have to read every commit to find those caveats, so the launcher lifts them into the PR body.

Pure and stdlib-only; the text is untrusted and is sanitised by the caller.
"""
import collections
import re

Record = collections.namedtuple("Record", "verified not_verified follow_ups")
EMPTY = Record([], [], [])

MAX_LINES_PER_MESSAGE = 400   # a commit message is read, not parsed forever
MAX_KEPT = 200                # per kind; rendering shows far fewer

_NOT_VERIFIED = re.compile(r"(?i)^\s*not\s+verified\b")
_VERIFIED = re.compile(r"(?i)^\s*verified\b")
_FOLLOW_UP = re.compile(r"(?i)^\s*follow-ups?\s*:\s*(\S.*)$")


def extract(messages):
    """[(short sha, full message)] -> Record, oldest commit first.

    verified / not_verified: the whole line, e.g. "Verified: pyflakes, pytest".
    follow_ups: the text after `Follow-up:`.
    """
    rec = Record([], [], [])
    for sha, message in messages:
        for line in message.splitlines()[:MAX_LINES_PER_MESSAGE]:
            if _NOT_VERIFIED.match(line):
                kind, text = rec.not_verified, line.strip()
            elif _VERIFIED.match(line):
                kind, text = rec.verified, line.strip()
            elif (m := _FOLLOW_UP.match(line)):
                kind, text = rec.follow_ups, m.group(1).strip()
            else:
                continue
            if len(kind) < MAX_KEPT:
                kind.append((sha, text))
    return rec


def is_empty(rec):
    return not any(rec)
