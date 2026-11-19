"""session_docs.py - the OKF docs a session reads on demand inside its container.

`docs/session/` holds the source: `common/` (concepts both backends share) and
one directory per backend kind (`github/`, `local/`) with that backend's own
concepts and its root `index.md`. A launch copies `common/` plus the matching
kind, flattened, to `~/.claude/docs/sandbox/` as one OKF bundle. The session's
CLAUDE.md names that path and says when to read what, so the always-loaded
prompt stays small and the rest is found when needed (spec: docs/dev/
architecture/session-docs.md).

Why assembled rather than two full bundles: most concepts are identical for
both backends, and a copy would drift. Why static files rather than rendered
templates: per-session facts (container name, mounts, devices) stay in
CLAUDE.md; these pages are the part that does not change per session. The text
comes from this checkout on the host, never from the repo being worked on, so
it is as trusted as CLAUDE.md. Pure: only reads files, returns bytes.
"""
import os

from claude_sandbox import paths
from claude_sandbox.container.incus import HOME

CONTAINER_DOCS = f"{HOME}/.claude/docs/sandbox"
DOCS_PATH = "~/.claude/docs/sandbox"      # as the session is told
COMMON = "common"
MAX_FILE = 200_000
MAX_TOTAL = 2_000_000


def _concepts(directory):
    """Sorted names of the regular, non-hidden .md files directly in `directory`
    (no symlinks: one could point anywhere on the host)."""
    if not os.path.isdir(directory):
        return []
    return sorted(n for n in os.listdir(directory)
                  if n.endswith(".md") and not n.startswith(".")
                  and os.path.isfile(os.path.join(directory, n))
                  and not os.path.islink(os.path.join(directory, n)))


def session_doc_files(kind, src=paths.SESSION_DOCS):
    """[(path in the container, bytes, mode)] for the bundle of backend `kind`.

    ValueError if the kind has no root `index.md`, if a name exists in both
    `common/` and the kind's directory (which one would win is a guess), or if
    `common/` has an `index.md` (a variant owns the index)."""
    kind_dir = os.path.join(src, kind)
    common = _concepts(os.path.join(src, COMMON))
    own = _concepts(kind_dir)
    if "index.md" not in own:
        raise ValueError(f"{kind_dir}/index.md is missing")
    if "index.md" in common:
        raise ValueError(f"{COMMON}/index.md: the index belongs to each backend")
    clash = sorted(set(common) & set(own))
    if clash:
        raise ValueError(f"in both {COMMON}/ and {kind}/: {', '.join(clash)}")
    files, total = [], 0
    for base, names in ((os.path.join(src, COMMON), common), (kind_dir, own)):
        for n in names:
            p = os.path.join(base, n)
            if os.path.getsize(p) > MAX_FILE:
                raise ValueError(f"{p} exceeds {MAX_FILE} bytes")
            with open(p, "rb") as f:
                data = f.read()
            total += len(data)
            if total > MAX_TOTAL:
                raise ValueError(f"session docs exceed {MAX_TOTAL} bytes")
            files.append((f"{CONTAINER_DOCS}/{n}", data, "0644"))
    return sorted(files)
