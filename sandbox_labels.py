"""sandbox_labels.py - the labels claude_sandbox puts on GitHub issues and PRs.

One table, so an issue filed from a `Sandbox-Action` trailer and a PR opened at
session exit are labelled the same way. Stdlib only. Labels are created on
first use, best effort: a repo that refuses (no push access) still gets the
issue or PR, which is why nothing here may be the only record of anything
(the issue marker and the commit comment are).
"""
import re

SANDBOX = "sandbox"              # everything we create
FOLLOW_UP = "follow-up"          # an issue from a Sandbox-Action trailer
CHECK_FAILED = "check-failed"    # a PR whose sandbox/check status is red

TABLE = {  # name -> (colour, description)
    SANDBOX: ("5319e7", "Filed by claude_sandbox from a session's commit"),
    FOLLOW_UP: ("fbca04", "A follow-up a sandboxed session asked for"),
    CHECK_FAILED: ("d73a4a", "sandbox/check reported errors for this PR's "
                             "head commit"),
}
SESSION_PREFIX = "session:"
_SESSION_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")


def session_label(session):
    """`session:<id>`, or None if `session` isn't a valid session name."""
    return SESSION_PREFIX + session if session and _SESSION_OK.match(session) else None


def colour_and_description(name):
    return TABLE.get(name, ("ededed", "claude_sandbox session"))


def ensure(api, repo, names, done=None):
    """Create any missing labels (once per `done` set). Never raises on a
    refusal: 422 means it exists, 403/404 means we may not create it."""
    done = set() if done is None else done
    for name in names:
        if name in done:
            continue
        color, desc = colour_and_description(name)
        try:
            api("POST", f"/repos/{repo}/labels",
                {"name": name, "color": color, "description": desc})
        except RuntimeError:
            pass
        done.add(name)
    return done
