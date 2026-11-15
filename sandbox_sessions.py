"""sandbox_sessions.py - identity of concurrent sessions on one repo.

Several sandboxes can work on the same repo at once. Each one is a *session*
named after its container, picked Docker-style (`quirky-hopper`). Everything a session does on
GitHub is scoped by that name:

- its deploy key is titled `ephemeral <host>:<session>`, so a later launch can
  tell whose key is stale (container gone) without touching a live sibling's;
- it works on branches under `sbx/<session>/`, never on the default branch.

A deploy key can push to any branch, so the branch prefix is a convention the
session is told to follow, not a permission. What actually protects the
default branch is a GitHub ruleset (see `rulesets/protect-main.json`);
`check_protection` only *reports* whether one is in place.

Pure helpers plus thin gh_api callers; no dependency on the rest of the
launcher beyond sandbox_github.
"""
import random
import re
import socket
from typing import Optional
import urllib.parse

from sandbox_github import GitHubError, gh_api

# Same alphabet as an incus instance name; <=40 so branch names stay short.
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")
BRANCH_ROOT = "sbx"
KEY_RE = re.compile(r"^ephemeral ([A-Za-z0-9.-]{1,63}):([A-Za-z0-9-]{1,40})$")


ADJECTIVES = (
    "admiring", "agile", "amiable", "bold", "brave", "breezy", "calm",
    "clever", "cosmic", "curious", "dazzling", "eager", "elegant", "fervent",
    "focused", "frosty", "gallant", "gentle", "happy", "hopeful", "jolly",
    "keen", "lively", "lucid", "mellow", "modest", "nimble", "noble",
    "peaceful", "quirky", "relaxed", "serene", "sharp", "silly", "sleepy",
    "stoic", "sunny", "tender", "upbeat", "vibrant", "witty", "zealous",
)
SURNAMES = (
    "agnesi", "babbage", "bartik", "boole", "brahe", "cerf", "curie",
    "darwin", "dijkstra", "euler", "faraday", "fermat", "feynman", "galileo",
    "germain", "godel", "hamilton", "hawking", "hopper", "hypatia", "kepler",
    "knuth", "lamport", "lovelace", "maxwell", "mendel", "newton", "noether",
    "pascal", "ramanujan", "ritchie", "shannon", "thompson", "torvalds",
    "turing", "volta", "wiles", "wozniak", "yonath", "zuse",
)


def pick_name(taken=(), rng=random):
    """A fresh `adjective-surname` session id, not in `taken`.

    Same idea as Docker's container names. After 20 collisions (a busy host)
    a short random suffix guarantees progress.
    """
    taken = set(taken)
    for _ in range(20):
        name = f"{rng.choice(ADJECTIVES)}-{rng.choice(SURNAMES)}"
        if name not in taken:
            return name
    while True:
        name = f"{rng.choice(ADJECTIVES)}-{rng.choice(SURNAMES)}-{rng.randrange(16**4):04x}"
        if name not in taken:
            return name


def validate_session(name):
    """Return name if usable as a session id (container name and branch
    component), else raise ValueError."""
    if not SESSION_RE.match(name):
        raise ValueError(
            f"invalid session name {name!r}: use 1-40 letters, digits and "
            "hyphens, starting with a letter or digit")
    return name


def branch_prefix(session):
    return f"{BRANCH_ROOT}/{session}/"


def work_branch(session):
    return branch_prefix(session) + "work"


def host_id(hostname=None):
    """This machine's name as it appears in key titles (host-safe chars)."""
    h = re.sub(r"[^A-Za-z0-9.-]", "-", hostname or socket.gethostname())
    return h.strip(".-")[:63] or "host"


def key_title(host, session):
    return f"ephemeral {host}:{session}"


def stale_keys(keys, host, live_sessions):
    """ids of deploy keys that belong to a dead session of *this* host.

    Only keys titled by this launcher for this host are candidates; keys from
    other hosts (their sessions may be alive) and titles we don't recognise
    (including the old `ephemeral <name>` format) are never touched.
    """
    live = set(live_sessions)
    out = []
    for k in keys:
        m = KEY_RE.match(k.get("title") or "")
        if m and m.group(1) == host and m.group(2) not in live:
            out.append(k["id"])
    return out


def reap_stale_keys(repo, host, live_sessions, delete=None):
    """Delete this host's deploy keys whose container no longer exists.

    A killed launcher (SIGKILL, host reboot) never runs teardown, so its key
    would live on with write access. `live_sessions` must come from the
    container runtime *after* our own container exists, so no live key is
    ever reaped. Returns the ids deleted.
    """
    keys = gh_api("GET", f"/repos/{repo}/keys?per_page=100") or []
    gone = stale_keys(keys, host, live_sessions)
    for key_id in gone:
        (delete or (lambda i: gh_api("DELETE", f"/repos/{repo}/keys/{i}")))(key_id)
    return gone


def protection_state(rule_types, classic_requires_pr):
    """'protected', 'unprotected' or 'unknown' (see protection_warning)."""
    if "pull_request" in (rule_types or ()) or classic_requires_pr:
        return "protected"
    if rule_types is None and classic_requires_pr is None:
        return "unknown"
    return "unprotected"


def protection_warning(branch, rule_types, classic_requires_pr):
    """Warning text if `branch` doesn't require pull requests, else None.

    rule_types: ruleset rule types active on the branch (None = unknown);
    classic_requires_pr: classic branch protection requires PRs (None = unknown).
    """
    if "pull_request" in (rule_types or ()) or classic_requires_pr:
        return None
    if rule_types is None and classic_requires_pr is None:
        return (f"couldn't verify protection of `{branch}`; sessions are told "
                "to use branches and PRs, but nothing here enforces it")
    return (f"`{branch}` does not require pull requests, so any session's "
            "deploy key (including a prompt-injected one) can push straight "
            "to it and concurrent sessions can race each other. Apply "
            "rulesets/protect-main.json (see README, 'concurrent sessions')")


def check_protection(repo):
    """(default_branch, warning or None, state) from the GitHub API.

    Read-only; errors other than 'not found' make the answer 'unknown', never
    an exception, because this is advice, not a gate.
    """
    info = gh_api("GET", f"/repos/{repo}")
    branch = info["default_branch"]
    quoted = urllib.parse.quote(branch, safe="")
    try:
        rules = gh_api("GET", f"/repos/{repo}/rules/branches/{quoted}") or []
        rule_types = {r.get("type") for r in rules}
    except GitHubError:
        rule_types = None
    classic: Optional[bool]
    try:
        prot = gh_api("GET", f"/repos/{repo}/branches/{quoted}/protection")
        classic = "required_pull_request_reviews" in (prot or {})
    except GitHubError as e:
        classic = False if e.code == 404 else None
    return (branch, protection_warning(branch, rule_types, classic),
            protection_state(rule_types, classic))
