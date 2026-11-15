"""sandbox_update.py - tell the user when this checkout is behind its upstream
and offer to `git pull`. Host side; never blocks a launch."""
import os
import subprocess
import sys

from sandbox_util import log

SKIP_ENV = "CLAUDE_SANDBOX_SKIP_UPDATE_CHECK"  # set on the post-pull re-exec
FETCH_TIMEOUT = 10
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}  # a credential prompt must never hang us


def _git(repo_dir, *args, timeout=None):
    return subprocess.run(["git", "-C", repo_dir, *args], text=True,
                          capture_output=True, timeout=timeout,
                          env={**os.environ, **GIT_ENV})


def behind(repo_dir):
    """(upstream, [one-line commits we lack]) after a fetch; None when there is
    nothing to report (not a git checkout, no upstream, up to date); the string
    'fetch-failed' when offline."""
    try:
        up = _git(repo_dir, "rev-parse", "--abbrev-ref", "@{u}")
        if up.returncode:
            return None
        try:
            fetch = _git(repo_dir, "fetch", "--quiet", timeout=FETCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            return "fetch-failed"
        if fetch.returncode:
            return "fetch-failed"
        new = _git(repo_dir, "log", "--oneline", "HEAD..@{u}")
    except FileNotFoundError:  # no git on PATH
        return None
    commits = new.stdout.splitlines() if not new.returncode else []
    return (up.stdout.strip(), commits) if commits else None


def check_for_updates(repo_dir, argv, interactive, ask, environ=None,
                      execv=os.execv):
    """Report if behind; if interactive and the user agrees, `git pull
    --ff-only` and re-exec `argv` so the new code is what runs.

    ask(prompt) -> str supplies the answer (default no). --ff-only never makes
    a merge commit and never touches diverged local work; a declined or failed
    pull just continues with the current version.
    """
    environ = os.environ if environ is None else environ
    if environ.get(SKIP_ENV):
        return
    found = behind(repo_dir)
    if found is None:
        return
    if found == "fetch-failed":
        log("note: couldn't check for updates (git fetch failed)")
        return
    up, commits = found
    log(f"NOTICE: this checkout is {len(commits)} commit(s) behind {up}.")
    for c in commits:
        log(f"  {c}")
    if not interactive:
        log(f"run 'git -C {repo_dir} pull' and re-run to use the latest version.")
        return
    if ask("git pull now? [y/N] ").strip().lower() not in ("y", "yes"):
        log("continuing with the current version.")
        return
    pull = _git(repo_dir, "pull", "--ff-only")
    sys.stderr.write(pull.stdout + pull.stderr)
    if pull.returncode:
        log("pull failed; continuing with the current version.")
        return
    log("updated; restarting.")
    environ[SKIP_ENV] = "1"
    execv(sys.executable, [sys.executable, *argv])
