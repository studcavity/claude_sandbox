"""session_tool.py - install the commit checker in the sandbox as `sandbox-check`."""

import os
from claude_sandbox.container.incus import HOME


SESSION_TOOL_DIR = f"{HOME}/.local/share/claude-sandbox"


SESSION_TOOL_BIN = f"{HOME}/.local/bin/sandbox-check"


# The checker script plus all of portable/ (the checker's logic and what it
# imports), mirroring the repo layout so the script finds its package the same
# way in the sandbox as in a checkout. portable/ is stdlib-only and imports
# nothing outside itself (a test enforces both).
SESSION_TOOL_SCRIPT = "scripts/sandbox_check.py"


SESSION_TOOL_PACKAGE = "claude_sandbox/portable"


SESSION_TOOL_WRAPPER = """#!/bin/sh
# Installed by claude_sandbox. Lints this session's own commits (everything
# since the remote's default branch) with the same rules CI and the launcher
# use. With arguments it is just scripts/sandbox_check.py.
set -eu
tool="${HOME}/.local/share/claude-sandbox/scripts/sandbox_check.py"
if [ "$#" -gt 0 ]; then exec python3 "$tool" "$@"; fi
# Best effort: without a fresh origin the "behind" check would be stale.
timeout 30 git fetch -q origin || echo "sandbox-check: fetch failed; behind-check may be stale" >&2
base=$(git rev-parse --verify -q origin/HEAD || git rev-parse --verify -q origin/main \
       || git rev-parse --verify -q origin/master) || {
  echo "sandbox-check: can't find origin's default branch" >&2; exit 2; }
exec python3 "$tool" --fresh "$base" HEAD
"""


def session_tool_archive(files):
    """The session tools as ONE gzipped tar (paths relative to `/`), so they
    arrive in a single exec instead of one per file. Deterministic (fixed
    mtime and owner), names are constants of ours, nothing user-controlled."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.USTAR_FORMAT,
                      compresslevel=6) as tar:
        for path, data, mode in files:
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size, info.mode, info.mtime = len(data), int(mode, 8), 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def session_tool_files(src_dir):
    """[(path in the container, bytes, mode)] for the in-sandbox checker."""
    portable = sorted(n for n in os.listdir(os.path.join(src_dir, SESSION_TOOL_PACKAGE))
                      if n.endswith(".py"))
    rel = [SESSION_TOOL_SCRIPT, "claude_sandbox/__init__.py",
           *(f"{SESSION_TOOL_PACKAGE}/{n}" for n in portable)]
    files = []
    for name in rel:
        with open(os.path.join(src_dir, name), "rb") as f:
            files.append((f"{SESSION_TOOL_DIR}/{name}", f.read(),
                          "0755" if name == SESSION_TOOL_SCRIPT else "0644"))
    files.append((SESSION_TOOL_BIN, SESSION_TOOL_WRAPPER.encode(), "0755"))
    return files
