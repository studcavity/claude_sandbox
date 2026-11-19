"""skills.py - Claude skills shipped to every session, kept as git submodules.

Skills live as submodules under `data/skills/` (today: the Open Knowledge
Format repo, whose `skills/` holds `okf-generate` and `okf-consume`). They are
cached and versioned by git but not vendored. On every launch the host runs
`git submodule update --init` so they are present at the commit this repo
pins (never upstream's moving branch: upstream changes, including skill
scripts, reach sessions only through a reviewed bump of the gitlink); the
launch stops only when that fails AND no skill is checked out at all (a stale
copy is better than none). `skill_files` then lists what to copy into the
container's `~/.claude/skills/`.

Why submodules rather than a library or a download: a skill is a directory of
Markdown and scripts that upstream changes often, and git already gives the
caching, pinning and offline fallback we want. Driven with invoke like
`github/gh.py` does for `gh`. Pure decisions (`decide`, `find_skills`) are
separate from the I/O (`refresh`).
"""
import os
import re
import shlex

from invoke import Context
from invoke.exceptions import CommandTimedOut

from claude_sandbox import paths
from claude_sandbox.container.incus import HOME

SKILLS_DIR = os.path.join(paths.DATA, "skills")
CONTAINER_SKILLS = f"{HOME}/.claude/skills"
TIMEOUT = 90
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}  # a credential prompt must never hang us
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_FILE = 1_000_000      # a skill is Markdown and small scripts
MAX_TOTAL = 10_000_000
MAX_DEPTH = 4             # <submodule>/skills/<name>/SKILL.md is depth 3


def refresh(root=paths.ROOT, skills_dir=SKILLS_DIR):
    """Init, fetch and update every submodule under `skills_dir` to the
    commit this repo pins. -> (ok, detail); never raises."""
    rel = os.path.relpath(skills_dir, root)
    cmd = shlex.join(["git", "-C", root, "submodule", "update", "--init",
                      "--depth", "1", "--", rel])
    try:
        r = Context().run(cmd, hide=True, warn=True, timeout=TIMEOUT,
                          env=GIT_ENV, in_stream=False)
    except CommandTimedOut:
        return False, f"timed out after {TIMEOUT}s"
    if r.exited == 127:
        return False, "git is not installed"
    if r.exited:
        return False, (r.stderr.strip() or r.stdout.strip())[-300:] or f"exit {r.exited}"
    return True, ""


def find_skills(skills_dir=SKILLS_DIR):
    """{name: directory} for every directory under `skills_dir` holding a
    SKILL.md (hidden directories skipped, not searched inside a skill). A
    name that is not a plain lowercase slug is ignored, so it can never be
    a path that escapes ~/.claude/skills. The first (sorted) one wins."""
    found: dict = {}
    base_depth = skills_dir.rstrip(os.sep).count(os.sep)
    for dirpath, dirs, files in os.walk(skills_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if dirpath.count(os.sep) - base_depth >= MAX_DEPTH:
            dirs[:] = []
        if "SKILL.md" in files and dirpath != skills_dir:
            name = os.path.basename(dirpath)
            if NAME_RE.match(name):
                found.setdefault(name, dirpath)
            dirs[:] = []
    return found


def decide(update_ok, have_skills):
    """'ok' | 'stale' (update failed, using what is checked out) | 'missing'
    (update failed and nothing is checked out: the launch must stop)."""
    if update_ok:
        return "ok"
    return "stale" if have_skills else "missing"


def skill_files(skills_dir=SKILLS_DIR):
    """[(path in the container, bytes, mode)] for every skill. Regular files
    only (a symlink could point anywhere on the host), nothing hidden or
    cached, and size-capped; sorted so the archive is deterministic."""
    files, total = [], 0
    for name, src in sorted(find_skills(skills_dir).items()):
        for dirpath, dirs, names in os.walk(src):
            dirs[:] = sorted(d for d in dirs
                             if not d.startswith(".") and d != "__pycache__"
                             and not os.path.islink(os.path.join(dirpath, d)))
            for n in sorted(names):
                p = os.path.join(dirpath, n)
                if n.startswith(".") or os.path.islink(p) or not os.path.isfile(p):
                    continue
                if os.path.getsize(p) > MAX_FILE:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
                total += len(data)
                if total > MAX_TOTAL:
                    raise ValueError(f"skills exceed {MAX_TOTAL} bytes")
                rel = os.path.relpath(p, src).replace(os.sep, "/")
                mode = "0755" if os.access(p, os.X_OK) else "0644"
                files.append((f"{CONTAINER_SKILLS}/{name}/{rel}", data, mode))
    return files
