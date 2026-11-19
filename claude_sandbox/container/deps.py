"""
container/deps.py - parse, validate and download a repo's claude_sandbox_deps.yaml.

Runs on the HOST. Nothing here executes downloaded content; claude_sandbox/cli.py
pushes the verified bytes into the container. Design rules:

  * strict schema: unknown keys, odd names and bad URLs are errors, not warnings
  * https only (including every redirect), no credentials in URLs, size caps
  * the GitHub token is never sent to a download host
  * binaries must carry a sha256; it is checked on the host before the bytes
    reach the container. Scripts may carry one (recommended).
  * `last_generated` and `platform` are inert metadata for humans/LLMs (when
    the file was written and on what OS); they are validated, never executed,
    and never part of the image cache key.
"""
import datetime
import hashlib
import hmac
import io
import json
import re
import tarfile
import urllib.parse
import urllib.request
import zipfile

import yaml

DEPS_FILE = "claude_sandbox_deps.yaml"
MAX_DEPS_FILE = 256 * 1024
MAX_SCRIPT = 1024 * 1024
MAX_BINARY = 256 * 1024 * 1024
MAX_PLATFORM = 4096
MAX_FILE = 16 * 1024 * 1024
MAX_FILES = 16
# Bump when the install steps in claude_sandbox/cli.py change, so cached
# per-repo images built by the old steps are not reused.
INSTALL_RECIPE_VERSION = "1"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
# Debian package name, optional =version. No leading '-' (option injection).
APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}(=[A-Za-z0-9][A-Za-z0-9.+:~-]{0,99})?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Variables that change how bash/the loader behave rather than configure a tool.
# Repo-relative script path: no leading '/' or '-', no '..' or '.' segments.
# Base image a repo may ask for: Ubuntu only (the base recipe is apt-based and
# tested on Ubuntu), from the two official Ubuntu sources. No other remotes, so
# a repo can't point the host at a private or local image.
IMAGE_RE = re.compile(r"^(images:ubuntu/|ubuntu:)[a-z0-9][a-z0-9.-]{0,31}$")
SETUP_PATH_RE = re.compile(r"^[A-Za-z0-9_+][A-Za-z0-9._+-]*(/[A-Za-z0-9_+][A-Za-z0-9._+-]*)*$")
SETUP_TIMEOUT_DEFAULT, SETUP_TIMEOUT_MAX, MAX_SETUP = 1800, 3600, 8
ENV_DENY = re.compile(r"^(LD_.*|PATH|IFS|ENV|BASH_ENV|BASHOPTS|SHELLOPTS|PS4|"
                      r"PROMPT_COMMAND|CDPATH|GLOBIGNORE)$")


# `files:` destination: under the session user's home, written `~/<relative>`.
# Plain path characters only; `.` / `..` segments are rejected in _files().
FILE_DEST_RE = re.compile(r"^~/[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*$")
# Host path a `files:` entry may read (same alphabet as a mount source).
FILE_HOST_RE = re.compile(r"^(~|/)[A-Za-z0-9._/+@ -]{1,300}$")
FILE_MODE_RE = re.compile(r"^0[0-7]{3}$")
ON_ERROR = ("fail", "skip")
# Places under ~ where a dropped-in file is code or credentials the session (or
# a later login) would run or use: shell startup, ssh/gpg/git/claude config,
# autostart. Matched as a path or a directory prefix. Best effort, not a
# sandbox: a session user can always edit its own home.
FILE_DEST_DENY = (
    ".ssh", ".gnupg", ".claude", ".claude.json", ".netrc", ".git-credentials",
    ".gitconfig", ".config/git", ".config/gh", ".bashrc", ".bash_profile",
    ".bash_login", ".bash_logout", ".profile", ".zshrc", ".zshenv",
    ".zprofile", ".zlogin", ".zlogout", ".pam_environment", ".tmux.conf",
    ".config/tmux", ".config/fish", ".config/systemd", ".config/autostart",
    ".config/environment.d")


# ISO 8601 date or date-time (UTC "Z" or numeric offset), nothing else.
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
                    r"(Z|[+-]\d{2}:?\d{2})?)?$")


class DepsError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# parsing / validation
# --------------------------------------------------------------------------- #
def _check_keys(item, where, required, optional=()):
    if not isinstance(item, dict):
        raise DepsError(f"{where}: expected a mapping, got {type(item).__name__}")
    extra = set(item) - set(required) - set(optional)
    missing = set(required) - set(item)
    if extra:
        raise DepsError(f"{where}: unknown key(s): {', '.join(sorted(map(str, extra)))}")
    if missing:
        raise DepsError(f"{where}: missing key(s): {', '.join(sorted(missing))}")


def _str(item, key, where, regex=None):
    v = item[key]
    if not isinstance(v, str) or (regex and not regex.match(v)):
        raise DepsError(f"{where}: invalid {key}: {v!r}")
    return v


def _url(item, where):
    u = _str(item, "url", where)
    p = urllib.parse.urlsplit(u)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise DepsError(f"{where}: url must be https:// with no credentials: {u!r}")
    return u


def _sha(item, where, required):
    if "sha256" not in item:
        if required:
            raise DepsError(f"{where}: sha256 is required")
        return None
    return _str(item, "sha256", where, SHA_RE).lower()  # lowercase 64 hex only


def _section(doc, key):
    v = doc.get(key) or []
    if not isinstance(v, list):
        raise DepsError(f"{key}: expected a list")
    return v


def _args(item, where):
    args = item.get("args") or []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise DepsError(f"{where}: args must be a list of strings")
    return args


def _env(item, where):
    env = item.get("env") or {}
    if not isinstance(env, dict):
        raise DepsError(f"{where}: env must be a mapping")
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_RE.match(k) or ENV_DENY.match(k):
            raise DepsError(f"{where}: env name not allowed: {k!r}")
        if not isinstance(v, (str, int, float, bool)) or "\0" in str(v):
            raise DepsError(f"{where}: env {k} must be a plain scalar")
    return {k: str(v) for k, v in env.items()}


def _repo_scripts(doc, key):
    """The `setup:` / `check:` section: repo scripts run in the container, as
    the session user, in the clone (never baked into an image)."""
    out = []
    for i, s in enumerate(_section(doc, key)):
        w = f"{key}[{i}]"
        _check_keys(s, w, ("name", "path"), ("args", "env", "timeout"))
        timeout = s.get("timeout", SETUP_TIMEOUT_DEFAULT)
        if (not isinstance(timeout, int) or isinstance(timeout, bool)
                or not 1 <= timeout <= SETUP_TIMEOUT_MAX):
            raise DepsError(f"{w}: timeout must be 1-{SETUP_TIMEOUT_MAX} seconds")
        path = _str(s, "path", w, SETUP_PATH_RE)
        if any(seg in (".", "..") for seg in path.split("/")):
            raise DepsError(f"{w}: invalid path: {path!r}")
        out.append({"name": _str(s, "name", w, NAME_RE), "path": path,
                    "args": _args(s, w), "env": _env(s, w),
                    "timeout": timeout})
    if len(out) > MAX_SETUP:
        raise DepsError(f"{key}: at most {MAX_SETUP} entries")
    if len({s["name"] for s in out}) != len(out):
        raise DepsError(f"{key}: duplicate names")
    return out


def _files(doc):
    """The `files:` section: pinned files placed under the session user's home.

    Each entry has exactly one source (`url`, fetched by the host, or
    `host_path`, read from the host and only with the host user's approval),
    a required sha256 that the host checks before the bytes reach the
    container, and a `~/` destination outside FILE_DEST_DENY. `on_error`
    says what a failed placement does: `fail` (default) aborts the launch,
    `skip` logs it and carries on without the file.
    """
    out = []
    for i, f in enumerate(_section(doc, "files")):
        w = f"files[{i}]"
        _check_keys(f, w, ("name", "dest", "sha256"),
                    ("url", "host_path", "mode", "on_error"))
        if ("url" in f) == ("host_path" in f):
            raise DepsError(f"{w}: give exactly one of url or host_path")
        dest = _str(f, "dest", w, FILE_DEST_RE)
        rel = dest[2:]
        if any(seg in (".", "..") for seg in rel.split("/")):
            raise DepsError(f"{w}: invalid dest: {dest!r}")
        for bad in FILE_DEST_DENY:
            if rel == bad or rel.startswith(bad + "/"):
                raise DepsError(f"{w}: dest {dest!r} is not allowed (it is "
                                f"under ~/{bad}: shell, credential or "
                                "autostart config)")
        mode = f.get("mode", "0644")
        if not isinstance(mode, str) or not FILE_MODE_RE.match(mode):
            raise DepsError(f"{w}: mode must be a quoted octal string such as "
                            f"\"0644\", got {mode!r}")
        on_error = f.get("on_error", "fail")
        if on_error not in ON_ERROR:
            raise DepsError(f"{w}: on_error must be one of "
                            f"{', '.join(ON_ERROR)}, got {on_error!r}")
        host_path = None
        if "host_path" in f:
            host_path = _str(f, "host_path", w, FILE_HOST_RE)
            if "\0" in host_path:
                raise DepsError(f"{w}: invalid host_path")
        out.append({"name": _str(f, "name", w, NAME_RE), "dest": dest,
                    "url": _url(f, w) if "url" in f else None,
                    "host_path": host_path,
                    "sha256": _sha(f, w, required=True),
                    "mode": mode, "on_error": on_error})
    if len(out) > MAX_FILES:
        raise DepsError(f"files: at most {MAX_FILES} entries")
    for key in ("name", "dest"):
        if len({f[key] for f in out}) != len(out):
            raise DepsError(f"files: duplicate {key}s")
    return out


def parse(raw):
    """bytes/str -> {"apt", "scripts", "binaries", "files", "setup", "check", + metadata}."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_DEPS_FILE:
            raise DepsError(f"{DEPS_FILE} is larger than {MAX_DEPS_FILE} bytes")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise DepsError(f"{DEPS_FILE} is not valid UTF-8") from None
    try:
        doc = yaml.safe_load(raw)  # safe_load: plain data only, no Python objects
    except yaml.YAMLError as e:
        raise DepsError(f"{DEPS_FILE}: invalid YAML: {e}") from None
    if doc is None:
        doc = {}
    _check_keys(doc, DEPS_FILE, (),
                ("image", "apt", "scripts", "binaries", "files", "setup", "check",
                 "last_generated", "platform"))

    last_generated = doc.get("last_generated")
    if isinstance(last_generated, (datetime.datetime, datetime.date)):
        last_generated = last_generated.isoformat()  # unquoted YAML timestamp
    if last_generated is not None and (
            not isinstance(last_generated, str)
            or not TIME_RE.match(last_generated)):
        raise DepsError(f"last_generated: expected an ISO 8601 timestamp, "
                        f"got {last_generated!r}")
    platform = doc.get("platform")
    if platform is not None and (
            not isinstance(platform, str) or len(platform) > MAX_PLATFORM
            or any(c < " " and c not in "\n\t" or c == "\x7f"
                   for c in platform)):
        raise DepsError(f"platform: expected text of at most {MAX_PLATFORM} "
                        "characters with no control characters")

    image = doc.get("image")
    if image is not None and (not isinstance(image, str)
                              or not IMAGE_RE.match(image)):
        raise DepsError(f"image: expected images:ubuntu/<release> or "
                        f"ubuntu:<release> (e.g. images:ubuntu/24.04), "
                        f"got {image!r}")

    apt = []
    for i, pkg in enumerate(_section(doc, "apt")):
        if not isinstance(pkg, str) or not APT_RE.match(pkg):
            raise DepsError(f"apt[{i}]: invalid package spec: {pkg!r}")
        apt.append(pkg)

    scripts = []
    for i, s in enumerate(_section(doc, "scripts")):
        w = f"scripts[{i}]"
        _check_keys(s, w, ("name", "url"), ("sha256", "args", "env"))
        scripts.append({"name": _str(s, "name", w, NAME_RE), "url": _url(s, w),
                        "sha256": _sha(s, w, required=False),
                        "args": _args(s, w), "env": _env(s, w)})

    binaries = []
    for i, b in enumerate(_section(doc, "binaries")):
        w = f"binaries[{i}]"
        _check_keys(b, w, ("name", "url", "sha256"), ("archive_path",))
        member = b.get("archive_path")
        if member is not None and (not isinstance(member, str) or not member
                                   or "\0" in member):
            raise DepsError(f"{w}: invalid archive_path: {member!r}")
        binaries.append({"name": _str(b, "name", w, NAME_RE), "url": _url(b, w),
                         "sha256": _sha(b, w, required=True),
                         "archive_path": member})

    names = [b["name"] for b in binaries]
    if len(set(names)) != len(names):
        raise DepsError("binaries: duplicate names")

    files = _files(doc)
    setup = _repo_scripts(doc, "setup")
    check = _repo_scripts(doc, "check")
    return {"image": image, "apt": apt, "scripts": scripts,
            "binaries": binaries, "files": files, "setup": setup,
            "check": check,
            "last_generated": last_generated, "platform": platform}


def cache_key(deps):
    """Hex digest identifying what gets installed, or None if it must not be
    baked into a cached image.

    Only the install sections feed the key (comments, ordering of keys, the
    metadata fields, `files` (placed per launch), `setup`/`check` (run in the container, never baked) and `image` don't;
    the image is already part of the deps image's alias via the base image). Scripts without a sha256 are whatever the URL
    serves at build time, and a cached image would freeze that with no way to
    notice, so such deps are never cached.
    """
    if not (deps["apt"] or deps["scripts"] or deps["binaries"]):
        return None
    if any(s["sha256"] is None for s in deps["scripts"]):
        return None
    blob = json.dumps({k: deps[k] for k in ("apt", "scripts", "binaries")},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(
        (INSTALL_RECIPE_VERSION + "\0" + blob).encode()).hexdigest()


def summary(deps):
    """One line per non-empty section, for logs and the sandbox CLAUDE.md."""
    out = []
    if deps.get("image"):
        out.append(f"image: `{deps['image']}` (the base OS image to launch)")
    if deps["apt"]:
        out.append("apt: " + ", ".join(f"`{p}`" for p in deps["apt"]))
    if deps["scripts"]:
        out.append("scripts: " + ", ".join(f"`{s['name']}`" for s in deps["scripts"]))
    if deps["binaries"]:
        out.append("binaries: " + ", ".join(
            f"`{b['name']}` (/usr/local/bin)" for b in deps["binaries"]))
    if deps.get("files"):
        out.append("files (placed under your home at the start of every "
                   "launch, sha256-pinned): " + ", ".join(
                       f"`{f['dest']}`" for f in deps["files"]))
    if deps.get("setup"):
        out.append("setup (run in the container at the start of every launch, "
                   "after the clone): " + ", ".join(
                       f"`{s['path']}`" for s in deps["setup"]))
    if deps.get("check"):
        out.append("check (the launcher runs these when you exit Claude, in "
                   "this container; if any fails you are resumed with the "
                   "output so you can fix it): " + ", ".join(
                       f"`{s['path']}`" for s in deps["check"]))
    return out


# --------------------------------------------------------------------------- #
# downloading
# --------------------------------------------------------------------------- #
class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise DepsError(f"refusing non-https redirect to {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpsOnly)


def fetch(url, max_bytes, sha256=None):
    """Download url (https only), enforce the size cap, verify sha256 if given."""
    try:
        with _opener.open(urllib.request.Request(
                url, headers={"User-Agent": "claude-sandbox"}), timeout=60) as r:
            buf, total = [], 0
            while chunk := r.read(1 << 20):
                total += len(chunk)
                if total > max_bytes:
                    raise DepsError(f"{url}: larger than {max_bytes} bytes")
                buf.append(chunk)
    except OSError as e:  # URLError, HTTPError, timeouts
        raise DepsError(f"{url}: download failed: {e}") from None
    data = b"".join(buf)
    if sha256 is not None:
        got = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(got, sha256):
            raise DepsError(f"{url}: sha256 mismatch (expected {sha256}, got {got})")
    return data


def extract_member(data, member, max_bytes=MAX_BINARY):
    """Read one named file out of a tar(.gz/.xz/.bz2) or zip, in memory only.

    Nothing is written to disk, so archive paths (../, absolute, symlinks)
    can't escape anywhere; only a regular file with that exact name is read.
    """
    bio = io.BytesIO(data)
    try:
        with tarfile.open(fileobj=bio) as tf:
            m = tf.getmember(member)
            if not m.isfile() or m.size > max_bytes:
                raise DepsError(f"archive member {member!r} is not a regular "
                                f"file of acceptable size")
            f = tf.extractfile(m)
            if f is None:  # not a regular file (already ruled out above)
                raise DepsError(f"archive member {member!r} is not readable")
            return f.read()
    except tarfile.ReadError:
        pass
    except KeyError:
        raise DepsError(f"archive has no member {member!r}") from None
    try:
        with zipfile.ZipFile(bio) as zf:
            info = zf.getinfo(member)
            if info.is_dir() or info.file_size > max_bytes:
                raise DepsError(f"archive member {member!r} is not a regular "
                                f"file of acceptable size")
            return zf.read(info)
    except zipfile.BadZipFile:
        raise DepsError("archive_path given but the download is not a "
                        "tar or zip archive") from None
    except KeyError:
        raise DepsError(f"archive has no member {member!r}") from None
