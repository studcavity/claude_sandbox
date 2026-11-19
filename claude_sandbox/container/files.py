"""container/files.py - place the deps file's `files:` entries in the container.

Runs on the HOST. The schema lives in deps.py (`_files`); this is what happens
to a parsed entry. Why it is not an install step in images.py: the files land
under the session user's home, may come from the host's disk, and need the
host user's say-so, so they are placed on every launch (after the clone,
before Claude's credentials arrive) and never baked into a cached image.

  * every entry is pinned: the sha256 is checked on the host against the exact
    bytes, whichever source they came from, before the container sees them
  * `url` entries are fetched like `binaries` (https only, size cap)
  * `host_path` entries read the host user's disk, which a repo must never do
    on its own: they are placed only with --allow-files or a yes at the prompt
    (no TTY and no flag means skipped), and --no-files ignores them all
  * a destination may not sit in or above the work directory (the clone is
    reviewed through git; files dropped in it would not be)
  * `on_error: skip` turns a failed placement into a logged note
"""
import hashlib
import hmac
import os
import posixpath
import stat

from claude_sandbox.container import deps as container_deps
from claude_sandbox.container.deps import DepsError

CONTAINER_HOME = "/home/ubuntu"


def dest_path(entry):
    """`~/a/b` -> `/home/ubuntu/a/b`."""
    return posixpath.join(CONTAINER_HOME, entry["dest"][2:])


def check_dest(entry, workdir=None):
    """Raise DepsError if the destination overlaps the work directory."""
    path = dest_path(entry)
    if workdir:
        w = workdir.rstrip("/")
        if path == w or path.startswith(w + "/") or w.startswith(path + "/"):
            raise DepsError(f"{entry['dest']} overlaps the work directory "
                            f"{workdir}")
    return path


def resolve(entries, allow_all, ask):
    """Split entries into (granted, declined).

    URL entries are granted (the host-verified sha256 is their approval, as
    for `binaries`). A host_path entry needs allow_all (--allow-files) or
    ask(entry) -> bool; `ask` is None when not interactive.
    """
    granted, declined = [], []
    for e in entries:
        if (e["url"] is not None or allow_all or (ask and ask(e))):
            granted.append(e)
        else:
            declined.append(e)
    return granted, declined


def read_host_file(path, max_bytes=container_deps.MAX_FILE):
    """Bytes of a regular host file (symlinks followed), size-capped."""
    p = os.path.expanduser(path)
    try:
        # O_NONBLOCK so a FIFO can't hang us; the fstat below rejects it.
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        raise DepsError(f"{path}: can't read: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DepsError(f"{path}: not a regular file")
        if st.st_size > max_bytes:
            raise DepsError(f"{path}: larger than {max_bytes} bytes")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)
    except OSError as e:
        raise DepsError(f"{path}: can't read: {e.strerror}") from None
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise DepsError(f"{path}: larger than {max_bytes} bytes")
    return data


def load(entry, fetch=None, read=read_host_file):
    """The verified bytes of one entry; DepsError if it can't be had or its
    sha256 doesn't match."""
    if entry["url"] is not None:
        fetch = fetch or container_deps.fetch  # looked up now, so tests can swap it
        return fetch(entry["url"], container_deps.MAX_FILE, entry["sha256"])
    data = read(entry["host_path"])
    got = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(got, entry["sha256"]):
        raise DepsError(f"{entry['host_path']}: sha256 mismatch "
                        f"(expected {entry['sha256']}, got {got})")
    return data


def place(box, entries, workdir, log, fetch=None, read=read_host_file):
    """Place granted entries; return one human line per entry.

    A failure raises DepsError for `on_error: fail` (the launch aborts) and is
    reported in the line for `skip`.
    """
    notes = []
    for e in entries:
        try:
            check_dest(e, workdir)
            data = load(e, fetch, read)
            box.put_user_file(e["dest"][2:], data, e["mode"])
        except (DepsError, OSError, ValueError) as err:
            if e["on_error"] != "skip":
                raise DepsError(f"files: {e['name']}: {err}") from None
            log(f"warning: file {e['name']} not placed ({err}); continuing")
            notes.append(f"`{e['dest']}`: **not placed** ({err}); "
                         "on_error is skip, so tell the user if you need it")
            continue
        except Exception as err:  # subprocess.CalledProcessError from the box
            if e["on_error"] != "skip":
                raise
            log(f"warning: file {e['name']} not placed ({err}); continuing")
            notes.append(f"`{e['dest']}`: **not placed** (writing it failed); "
                         "on_error is skip, so tell the user if you need it")
            continue
        notes.append(f"`{e['dest']}`: placed (mode {e['mode']})")
    return notes
