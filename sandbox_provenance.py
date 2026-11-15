"""sandbox_provenance.py - prove a PR's commits were made inside its session.

    python sandbox_provenance.py OWNER/REPO --pr N     # re-check and re-post

At launch the container makes an ed25519 *signing* key and git signs every
commit with it. The launcher keeps the PUBLIC half on the host (never in the
repo, where the session could change it); the private half dies with the
container. After teardown the launcher checks that every commit a session PR
adds over the default branch has a good signature from that session's key,
or from one of the user's own SSH signing keys registered on GitHub (so the
user's own rebases and fixups pass), and posts the result as the commit
status `sandbox/provenance` on the PR's head commit. A ruleset can require
it (rulesets/require-sandbox-provenance.json).

A status belongs to one commit. Anything pushed later (by a sibling
session's deploy key, or anyone else with push access) leaves the new head
without a status, so a required check blocks the merge until this is re-run
and passes, or someone overrides it.

What it proves: every commit in the range was made by this session or by the
user. What it does NOT prove: that the session's own commits are benign (the
session holds its key; reviewing the diff is still the gate), or that CI
didn't already run the extra commits (`pull_request: synchronize` runs on
every push, before any merge).

Why some of this is our own code: git does the cryptography (`%G?`/`%GF`
against `gpg.ssh.allowedSignersFile`, which shells out to ssh-keygen); what
is ours is picking the keys, the commit range and the status, which nothing
existing does for per-session keys.
"""
import argparse
import base64
import collections
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse

import sandbox_check
import sandbox_names

CONTEXT = "sandbox/provenance"
DESC_MAX = 140
STATE_ENV = "CLAUDE_SANDBOX_STATE_DIR"
SIGNING_KEY = "~/.ssh/signing_ed25519"   # inside the container
_REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_KEY_RE = re.compile(r"^(ssh-ed25519) ([A-Za-z0-9+/]+={0,2})(?: .*)?$")

# One commit as git reports it. status is %G?: G good, U good but key not
# allowed, N none, B bad, E can't check, X/Y/R expired/revoked.
Commit = collections.namedtuple("Commit", "sha status fingerprint parents")
Verdict = collections.namedtuple("Verdict", "state description problems sha")
Outcome = collections.namedtuple("Outcome", "pr state detail")


class ProvenanceError(ValueError):
    """A refusal to check or post, for the person running us."""


# --- keys (pure) ------------------------------------------------------------ #
def parse_pubkey(text):
    """'ssh-ed25519 <blob> [comment]' -> 'ssh-ed25519 <blob>', or ValueError.
    The blob must really be an ed25519 public key (type string + 32 bytes),
    since it ends up in a file ssh-keygen parses."""
    m = _KEY_RE.match((text or "").strip())
    if not m:
        raise ValueError("not an ssh-ed25519 public key")
    try:
        blob = base64.b64decode(m.group(2), validate=True)
    except ValueError:
        raise ValueError("public key is not valid base64") from None
    fields = []
    i = 0
    while i + 4 <= len(blob):
        n = int.from_bytes(blob[i:i + 4], "big")
        fields.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    if i != len(blob) or len(fields) != 2 or fields[0] != b"ssh-ed25519" \
            or len(fields[1]) != 32:
        raise ValueError("malformed ssh-ed25519 public key")
    return f"{m.group(1)} {m.group(2)}"


def fingerprint(pubkey):
    """SHA256 fingerprint as `ssh-keygen -l` and git's %GF print it."""
    blob = base64.b64decode(pubkey.split()[1])
    return "SHA256:" + base64.b64encode(
        hashlib.sha256(blob).digest()).decode().rstrip("=")


def allowed_signers(pubkeys):
    """An allowed-signers file accepting these keys for git signatures from
    any committer identity (we match keys, not email addresses)."""
    return "".join(f'* namespaces="git" {k}\n' for k in pubkeys)


# --- the host-side key store ---------------------------------------------------- #
def store_root(environ=None):
    environ = os.environ if environ is None else environ
    if environ.get(STATE_ENV):
        return os.path.join(environ[STATE_ENV], "signing")
    base = environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "claude_sandbox", "signing")


def key_path(repo, session, root=None):
    if not _REPO_RE.match(repo or "") or ".." in repo:
        raise ValueError(f"bad repository name {repo!r}")
    if not sandbox_names.SESSION_RE.match(session or ""):
        raise ValueError(f"bad session name {session!r}")
    owner, name = repo.lower().split("/")
    return os.path.join(root or store_root(), owner, name, f"{session}.pub")


def save_key(repo, session, pubkey, root=None):
    """Record a session's public signing key -> True if it replaced another
    session's key of the same name (whose PRs then fail closed)."""
    key = parse_pubkey(pubkey)
    path = key_path(repo, session, root)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    replaced = os.path.exists(path)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(key + "\n")
    os.replace(tmp, path)
    return replaced


def load_key(repo, session, root=None):
    """The recorded key, or None (no such session here, or it's unreadable)."""
    try:
        with open(key_path(repo, session, root)) as f:
            return parse_pubkey(f.read())
    except (OSError, ValueError):
        return None


def recorded_sessions(repo, root=None):
    """Session names with a recorded key for `repo`: a new session must not
    reuse one, or its key would vouch for the old session's branches."""
    try:
        d = os.path.dirname(key_path(repo, "x", root))
        return {n[:-4] for n in os.listdir(d) if n.endswith(".pub")}
    except (OSError, ValueError):
        return set()


# --- container side ------------------------------------------------------------ #
def container_script():
    """Shell (run as the session user) that makes the signing key, makes git
    sign every commit and tag with it, and prints the public half."""
    k = SIGNING_KEY
    return (f"ssh-keygen -q -t ed25519 -N '' -C sandbox-signing -f {k} && "
            "git config --global gpg.format ssh && "
            f"git config --global user.signingkey {k}.pub && "
            "git config --global commit.gpgsign true && "
            "git config --global tag.gpgsign true && "
            f"cat {k}.pub")


# --- judging a range (pure) ------------------------------------------------------ #
WHY = {"N": "not signed", "U": "signed by a key that isn't this session's "
       "or yours", "B": "bad signature", "E": "signature can't be checked",
       "X": "expired signature", "Y": "signature by an expired key",
       "R": "signature by a revoked key"}


def judge(commits, allowed):
    """-> [(sha, problem)] for every commit that doesn't check out. `allowed`
    maps fingerprint -> label. An empty range fails: nothing was proven."""
    if not commits:
        return [("", "no commits to check")]
    out = []
    for c in commits:
        if len(c.parents) > 1:
            out.append((c.sha, "merge commit"))
        elif c.status != "G":
            out.append((c.sha, WHY.get(c.status, f"signature status {c.status!r}")))
        elif c.fingerprint not in allowed:
            out.append((c.sha, "signed by a key that isn't this session's or yours"))
    return out


def describe(commits, problems, session):
    if not problems:
        return (f"{len(commits)} commit{'s' * (len(commits) != 1)} signed by "
                f"session {session} (or you)")
    sha, why = problems[0]
    more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
    return f"{sha[:7] or '-'}: {why}{more}"[:DESC_MAX]


# --- git and GitHub plumbing ------------------------------------------------------ #
def read_commits(base, head, signers_file, cwd):
    """Commits in base..head with their signature verdicts. The user's git
    config is shut out and the verifier pinned: a `gpg.ssh.program` that
    just prints "Good signature" makes git report G for any commit."""
    for r in (base, head):
        if not re.fullmatch(r"[0-9a-f]{40}", r or ""):
            raise ProvenanceError(f"not a commit id: {r!r}")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    raw = subprocess.run(
        ["git", "-c", f"gpg.ssh.allowedSignersFile={signers_file}",
         # a different program could simply print "Good signature": pin it
         "-c", "gpg.ssh.program=ssh-keygen", "log",
         "-z", "--format=%H%x1f%G?%x1f%GF%x1f%P", f"{base}..{head}"],
        cwd=cwd, env=env, check=True, capture_output=True, text=True,
        errors="replace").stdout
    out = []
    for rec in raw.split("\0"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 4:
            out.append(Commit(parts[0], parts[1], parts[2], parts[3].split()))
    return out


def user_signing_keys(api, login):
    """The user's SSH signing keys on GitHub (public data). GPG-signed commits
    by the user aren't accepted; sign with an SSH key or override."""
    keys = []
    for k in api("GET", f"/users/{urllib.parse.quote(login)}/ssh_signing_keys"
                 "?per_page=100") or []:
        try:
            keys.append(parse_pubkey(k.get("key", "")))
        except ValueError:
            continue   # RSA/ECDSA keys: not supported here
    return keys


def default_tip(api, repo):
    default = api("GET", f"/repos/{repo}")["default_branch"]
    b = api("GET", f"/repos/{repo}/branches/{urllib.parse.quote(default, safe='')}")
    return b["commit"]["sha"]


def check_pr(repo, number, api, client, root=None, fetch=None):
    """Verify one PR -> Verdict (nothing posted)."""
    pr = client.pr(repo, number)
    if (pr.head_repo or "").lower() != repo.lower():
        raise ProvenanceError(f"PR #{number} comes from {pr.head_repo or 'a fork'}")
    session = sandbox_check.branch_session(pr.head_ref)
    if not session:
        raise ProvenanceError(f"PR #{number} isn't from a sandbox branch")
    key = load_key(repo, session, root)
    if key is None:
        return Verdict("failure", f"no signing key recorded for session "
                       f"{session} on this host"[:DESC_MAX],
                       [("", "no key")], pr.head_sha)
    keys = [key] + [k for k in user_signing_keys(api, client.login()) if k != key]
    allowed = {fingerprint(k): k for k in keys}
    base = default_tip(api, repo)
    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = os.path.join(tmp, "repo.git")
        (fetch or sandbox_check.fetch_pr)(
            f"https://github.com/{repo}.git", number, base, repo_dir)
        signers = os.path.join(tmp, "allowed_signers")
        with open(signers, "w") as f:
            f.write(allowed_signers(keys))
        commits = read_commits(base, pr.head_sha, signers, repo_dir)
    problems = judge(commits, allowed)
    return Verdict("failure" if problems else "success",
                   describe(commits, problems, session), problems, pr.head_sha)


def post_pr(repo, number, api=None, client=None, root=None, fetch=None):
    """Check one PR and post `sandbox/provenance` on its head -> Verdict.
    A crash posts `error`, never `success`; nothing is posted if the head
    moved while checking (the verdict would be for the wrong commit)."""
    if api is None:
        import sandbox_github
        api = sandbox_github.gh_api
    if client is None:
        import sandbox_ghclient
        client = sandbox_ghclient.GhClient.from_environment()
    if not _REPO_RE.match(repo or ""):
        raise ProvenanceError(f"bad repository name {repo!r}")
    try:
        verdict = check_pr(repo, number, api, client, root, fetch)
    except ProvenanceError:
        raise
    except Exception as e:  # a broken check must never read as success
        sha = client.pr(repo, number).head_sha
        verdict = Verdict("error", f"check crashed ({type(e).__name__}); "
                          "re-run it", [("", str(e)[:200])], sha)
    pr = client.pr(repo, number)
    if pr.head_sha != verdict.sha:
        raise ProvenanceError(f"PR #{number} head moved while checking; "
                              "nothing posted. Re-run.")
    client.post_status(repo, verdict.sha, verdict.state, CONTEXT,
                       verdict.description, pr.url)
    return verdict


def post_session(repo, landed, api=None, client=None, root=None, poster=None):
    """Post `sandbox/provenance` for each PR a landing opened or found ->
    [Outcome]. One PR failing never affects the others."""
    out = []
    for n in sandbox_check.pr_numbers(landed):
        try:
            v = (poster or post_pr)(repo, n, api=api, client=client, root=root)
            out.append(Outcome(n, v.state, v.description))
        except Exception as e:
            out.append(Outcome(n, "failed", str(e)[:200]))
    return out


def format_outcomes(outcomes):
    if not outcomes:
        return ""
    lines = [f"{CONTEXT} status"]
    for o in outcomes:
        lines.append(f"  #{o.pr}: {o.state}" + (f" ({o.detail})" if o.detail else ""))
        if o.state != "success":
            lines.append("     re-check after fixing: python sandbox_provenance.py "
                         f"OWNER/REPO --pr {o.pr}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", metavar="OWNER/REPO")
    ap.add_argument("--pr", type=int, required=True, metavar="N")
    a = ap.parse_args(argv)
    try:
        v = post_pr(a.repo, a.pr)
    except (ProvenanceError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for sha, why in v.problems:
        print(f"  {sha[:7] or '-'}: {why}")
    print(f"posted {CONTEXT} {v.state} on {v.sha[:7]}: {v.description}")
    return 0 if v.state == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
