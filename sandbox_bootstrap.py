"""sandbox_bootstrap.py - make sure the launcher runs in a venv that has
requirements.txt installed. Standard library only: it runs before anything
else is imported, so run.sh can stay a one-line shim."""
import fcntl
import hashlib
import os
import subprocess
import sys

VENV = "venv"
STAMP = ".requirements.sha256"
LOCK = ".venv.lock"  # beside venv/: concurrent launches must not both build it


def requirements_digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def stamp_is_current(venv_dir, digest):
    try:
        with open(os.path.join(venv_dir, STAMP)) as f:
            return f.read().strip() == digest
    except FileNotFoundError:
        return False


def ensure_venv(script_dir, argv=None, python=None, prefix=None, base_prefix=None,
                run=subprocess.run, execv=os.execv, environ=None):
    """Create `<script_dir>/venv` and install requirements.txt when missing or
    changed, then re-exec this program under it. Returns normally (without
    doing anything) when already inside a virtualenv, so a developer's own
    venv, CI and the re-exec'd process are all left alone.

    Reinstalls only when requirements.txt changed (tracked by a hash stamp),
    instead of running pip on every launch.
    """
    environ = os.environ if environ is None else environ
    prefix = sys.prefix if prefix is None else prefix
    base_prefix = sys.base_prefix if base_prefix is None else base_prefix
    if prefix != base_prefix or environ.get("CLAUDE_SANDBOX_NO_VENV"):
        return
    python = python or sys.executable
    argv = sys.argv if argv is None else argv
    venv_dir = os.path.join(script_dir, VENV)
    venv_py = os.path.join(venv_dir, "bin", "python")
    req = os.path.join(script_dir, "requirements.txt")
    digest = requirements_digest(req)
    try:
        # Two sessions started together would otherwise create/pip-install
        # into the same venv at once. The second waits, then finds it ready.
        with open(os.path.join(script_dir, LOCK), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not os.path.exists(venv_py):
                print(f"[sandbox] creating {venv_dir}", file=sys.stderr)
                run([python, "-m", "venv", venv_dir], check=True)
            if not stamp_is_current(venv_dir, digest):
                print("[sandbox] installing requirements", file=sys.stderr)
                run([venv_py, "-m", "pip", "install", "-q", "-r", req],
                    check=True)
                with open(os.path.join(venv_dir, STAMP), "w") as f:
                    f.write(digest + "\n")
    except (subprocess.CalledProcessError, OSError) as e:
        raise SystemExit(f"could not set up {venv_dir}: {e}\n"
                         "(on Debian/Ubuntu: apt install python3-venv)") from None
    execv(venv_py, [venv_py, *argv])
