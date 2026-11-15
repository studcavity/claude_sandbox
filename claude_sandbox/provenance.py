"""provenance.py - prove a PR's commits were made inside its session.

    python provenance.py OWNER/REPO --pr N     # re-check and re-post
    python provenance.py OWNER/REPO --pr N --override REASON

At launch the container makes an ed25519 *signing* key and git signs every
commit with it. The launcher keeps the PUBLIC half on the host (never in the
repo, where the session could change it); the private half dies with the
container. After teardown the launcher checks that every commit a session PR
adds over the default branch has a good signature from that session's key,
or from one of the user's own SSH signing keys registered on GitHub (so the
user's own rebases and fixups pass), and posts the result as the commit
status `sandbox/provenance` on the PR's head commit. A ruleset can require
it (rulesets/require-sandbox-provenance.json).

--override REASON is the audited break-glass path (the same shape as
a status override elsewhere): it posts `success` for that one head commit
with an `OVERRIDDEN by <login>: <reason>` description and comments on the PR,
without checking signatures. Use it for commits the check can't vouch for
though you do, e.g. a rebase made with GitHub's web button (signed by GitHub,
not by the session or your SSH signing key).

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

from claude_sandbox.portable import names
from claude_sandbox.portable import untrusted
CONTEXT = "sandbox/provenance"
DESC_MAX = 140
STATE_ENV = "CLAUDE_SANDBOX_STATE_DIR"
SIGNING_KEY = "~/.ssh/signing_ed25519"   # inside the container
MIN_OVERRIDE_REASON = 10
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
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
    if not names.SESSION_RE.match(session or ""):
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


def override_description(login, reason):
    """The status description for a break-glass override (<= DESC_MAX chars).
    Raises ProvenanceError when the reason is unusable."""
    reason = " ".join(untrusted.printable(reason).split())
    if len(reason) < MIN_OVERRIDE_REASON:
        raise ProvenanceError(f"--override needs a real reason (>= "
                              f"{MIN_OVERRIDE_REASON} characters)")
    if untrusted.find_secrets(reason):
        raise ProvenanceError("--override reason looks like it contains a "
                              "credential; not posted")
    who = login if LOGIN_RE.match(login or "") else "unknown"
    return untrusted.truncate(f"OVERRIDDEN by {who}: {reason}", DESC_MAX)


def fetch_pr(url, number, base_sha, dest):
    """Fetch a PR's head and its base commit into a fresh bare repository at
    `dest`, authenticating through the user's gh login (no token handled)."""
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha or ""):
        raise ProvenanceError(f"unexpected base commit id {base_sha!r}")
    try:
        subprocess.run(["git", "init", "-q", "--bare", dest], check=True,
                       capture_output=True, text=True, errors="replace")
        subprocess.run(
            ["git", "-C", dest, "-c", "credential.helper=",
             "-c", "credential.helper=!gh auth git-credential",
             "fetch", "-q", "--no-tags", url,
             f"+refs/pull/{int(number)}/head:refs/pr/head", base_sha],
            check=True, capture_output=True, text=True, errors="replace")
    except subprocess.CalledProcessError as e:
        raise ProvenanceError(f"couldn't fetch PR #{int(number)} from {url}: "
                              f"{(e.stderr or '').strip()[-200:]}") from None


def pr_numbers(landed):
    """PR numbers of the open PRs a landing produced (duck-typed on
    `.outcome` and `.detail`, which holds the PR URL for opened/exists)."""
    nums = []
    for l in landed:
        tail = (l.detail or "").rstrip("/").rsplit("/", 1)[-1]
        if l.outcome in ("opened", "exists") and tail.isdigit():
            nums.append(int(tail))
    return nums


def check_pr(repo, number, api, client, root=None, fetch=None):
    """Verify one PR -> Verdict (nothing posted)."""
    pr = client.pr(repo, number)
    if (pr.head_repo or "").lower() != repo.lower():
        raise ProvenanceError(f"PR #{number} comes from {pr.head_repo or 'a fork'}")
    session = names.branch_session(pr.head_ref)
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
        (fetch or fetch_pr)(
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
        from claude_sandbox.github import gh as gh
        api = gh.gh_api
    if client is None:
        from claude_sandbox.github import client as ghclient
        client = ghclient.GhClient.from_environment()
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


def override_comment(login, reason, sha):
    """PR comment that makes an override auditable."""
    who = login if LOGIN_RE.match(login or "") else "unknown"
    return "\n".join([
        f"**`{CONTEXT}` overridden** by `{who}` for commit `{sha[:7]}`.", "",
        "Reason: " + untrusted.one_line(reason, 500), "",
        "Signatures were not checked. This applies to this commit only: a new "
        "commit needs a new check (or a new override)."])


def override_pr(repo, number, reason, client=None):
    """Break glass: post `success` on this PR's head with the reason, and
    comment on the PR -> Verdict. Same-repo sandbox-branch PRs only; nothing is
    posted if the reason is unusable or the head moved."""
    if client is None:
        from claude_sandbox.github import client as ghclient
        client = ghclient.GhClient.from_environment()
    if not _REPO_RE.match(repo or ""):
        raise ProvenanceError(f"bad repository name {repo!r}")
    pr = client.pr(repo, number)
    if (pr.head_repo or "").lower() != repo.lower():
        raise ProvenanceError(f"PR #{number} comes from {pr.head_repo or 'a fork'}")
    if not names.branch_session(pr.head_ref):
        raise ProvenanceError(f"PR #{number} isn't from a sandbox branch")
    login = client.login()
    desc = override_description(login, reason)
    if client.pr(repo, number).head_sha != pr.head_sha:
        raise ProvenanceError(f"PR #{number} head moved; nothing posted. Re-run.")
    client.post_status(repo, pr.head_sha, "success", CONTEXT, desc, pr.url)
    client.comment(repo, number, override_comment(login, reason, pr.head_sha))
    return Verdict("success", desc, [], pr.head_sha)


def post_session(repo, landed, api=None, client=None, root=None, poster=None):
    """Post `sandbox/provenance` for each PR a landing opened or found ->
    [Outcome]. One PR failing never affects the others."""
    out = []
    for n in pr_numbers(landed):
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
            lines.append("     re-check after fixing: python provenance.py "
                         f"OWNER/REPO --pr {o.pr}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repo", metavar="OWNER/REPO")
    ap.add_argument("--pr", type=int, required=True, metavar="N")
    ap.add_argument("--override", metavar="REASON",
                    help="break glass: post success for this PR's head commit "
                    "only, with the reason, and comment on the PR")
    a = ap.parse_args(argv)
    try:
        v = (override_pr(a.repo, a.pr, a.override) if a.override
             else post_pr(a.repo, a.pr))
    except (ProvenanceError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for sha, why in v.problems:
        print(f"  {sha[:7] or '-'}: {why}")
    print(f"posted {CONTEXT} {v.state} on {v.sha[:7]}: {v.description}")
    return 0 if v.state == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
