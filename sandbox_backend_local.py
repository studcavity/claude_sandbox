"""sandbox_backend_local.py - the local backend: a git repo on the host.

`./run.sh --local PATH` works on a git repository on this machine, with no
GitHub (or any other remote) involved. See docs/specs/08-local-backend.md.

The trust boundary is a **git bundle** in each direction:
- in: the host bundles ONE branch of the primary repo (the user's own, so
  trusted) and streams it into the container, which clones it onto the
  size-capped work volume. Nothing is bind-mounted; the container never sees
  the primary's .git (refs, config, hooks).
- out: after the session, the container bundles its `sbx/<session>/*`
  branches and streams them to the host. The host fetches that FILE into the
  primary with fsck on and one explicit refspec per validated ref. Fetching
  from a bundle runs nothing from the producing repo (no config, no hooks, no
  upload-pack), so nothing the session wrote runs on the host, and no ref
  outside `refs/heads/sbx/<session>/` can be created or moved.

Pure decisions (ids, ref filtering, argv) are separate from the I/O so they
can be tested without a container.
"""
import hashlib
import os
import re

import sandbox_names
import sandbox_provenance

BUNDLE_REF_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64}) (\S+)$")
# One path component of a branch name we accept back. Stricter than git's
# check-ref-format on purpose: these names reach the user's terminal and repo.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
MAX_REF_DEPTH = 8
MAX_BUNDLE_REFS = 100
DEFAULT_MAX_BUNDLE = 2 * 1024 ** 3   # bytes; bundles out are capped at this


class LocalError(RuntimeError):
    """A local-backend step failed; the message is safe to show the user."""


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def repo_id(path):
    """Stable `local/<name>-<hash>` id for the repo at `path` (already a
    realpath): the basename for people, a hash of the full path so two repos
    with one name don't share signing keys. Fits sandbox_provenance's
    `owner/name` rule (no `..`, limited alphabet)."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(path.rstrip("/")))
    base = re.sub(r"\.{2,}", ".", base).strip(".-")[:60] or "repo"
    digest = hashlib.sha256(path.encode()).hexdigest()[:8]
    return f"local/{base}-{digest}"


def key_root(environ=None):
    """Signing keys of local sessions live beside GitHub's, under their own
    root, so `local/<x>` can never collide with a GitHub owner called local."""
    return os.path.join(os.path.dirname(sandbox_provenance.store_root(environ)),
                        "signing-local")


# --------------------------------------------------------------------------- #
# what comes back
# --------------------------------------------------------------------------- #
def acceptable_ref(ref, session):
    """Whether `ref` (full name) may be imported for `session`: only
    refs/heads/sbx/<session>/<name>, every component of <name> plain."""
    prefix = "refs/heads/" + sandbox_names.BRANCH_ROOT + f"/{session}/"
    if not sandbox_names.SESSION_RE.match(session or "") or not ref.startswith(prefix):
        return False
    parts = ref[len(prefix):].split("/")
    return (0 < len(parts) <= MAX_REF_DEPTH
            and all(_COMPONENT_RE.match(p) and not p.endswith((".lock", "."))
                    and ".." not in p for p in parts))


def parse_heads(text, session):
    """`git bundle list-heads` output -> (accepted [(sha, ref)], rejected
    [text]). Anything that isn't a well-formed line for one of this session's
    branches is rejected (and never fetched); so is everything past
    MAX_BUNDLE_REFS."""
    accepted: list = []
    rejected: list = []
    seen: set = set()
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = BUNDLE_REF_RE.match(line.strip())
        if not m or not acceptable_ref(m.group(2), session) or m.group(2) in seen:
            rejected.append(line.strip()[:200])
            continue
        if len(accepted) >= MAX_BUNDLE_REFS:
            rejected.append(m.group(2))
            continue
        seen.add(m.group(2))
        accepted.append((m.group(1), m.group(2)))
    return accepted, rejected


def fetch_argv(primary, bundle, refs):
    """Fetch exactly `refs` from the bundle FILE into the same names in the
    primary. No `+`: a ref that already exists there only fast-forwards, so an
    import never rewrites what the user has since done to a branch. fsck
    rejects malformed objects."""
    if not refs:
        raise ValueError("no refs to fetch")
    for r in refs:
        if not r.startswith("refs/heads/"):
            raise ValueError(f"refusing to fetch {r!r}")
    return ["git", "-C", primary,
            "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true",
            "fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
            os.path.abspath(bundle), *[f"{r}:{r}" for r in refs]]


def bundle_out_argv(workdir, session, base_sha):
    """Run in the container: this session's branches, minus what the primary
    already has (the base commit), to stdout."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha or ""):
        raise ValueError(f"not a commit id: {base_sha!r}")
    if not sandbox_names.SESSION_RE.match(session or ""):
        raise ValueError(f"bad session name {session!r}")
    return ["git", "-C", workdir, "bundle", "create", "-",
            f"--branches={sandbox_names.BRANCH_ROOT}/{session}/*", f"^{base_sha}"]


def bundle_in_argv(primary, branch):
    """Run on the host: one branch of the primary, to stdout."""
    return ["git", "-C", primary, "bundle", "create", "-",
            f"refs/heads/{branch}"]


def copy_capped(src, dst, limit, chunk=1 << 20):
    """Copy a stream until EOF; raise LocalError once more than `limit` bytes
    arrive (the caller deletes the partial file). -> bytes copied."""
    total = 0
    while True:
        buf = src.read(chunk)
        if not buf:
            return total
        total += len(buf)
        if total > limit:
            raise LocalError(f"bundle is larger than the {limit}-byte cap")
        dst.write(buf)
