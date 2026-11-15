"""
sandbox_deps.py - parse, validate and download a repo's claude_sandbox_deps.yaml.

Runs on the HOST. Nothing here executes downloaded content; claude_sandbox.py
pushes the verified bytes into the container. Design rules:

  * strict schema: unknown keys, odd names and bad URLs are errors, not warnings
  * https only (including every redirect), no credentials in URLs, size caps
  * the GitHub token is never sent to a download host
  * binaries must carry a sha256; it is checked on the host before the bytes
    reach the container. Scripts may carry one (recommended).
"""
import hashlib
import hmac
import io
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

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
# Debian package name, optional =version. No leading '-' (option injection).
APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,99}(=[A-Za-z0-9][A-Za-z0-9.+:~-]{0,99})?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Variables that change how bash/the loader behave rather than configure a tool.
ENV_DENY = re.compile(r"^(LD_.*|PATH|IFS|ENV|BASH_ENV|BASHOPTS|SHELLOPTS|PS4|"
                      r"PROMPT_COMMAND|CDPATH|GLOBIGNORE)$")


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


def parse(raw):
    """bytes/str -> {"apt": [...], "scripts": [...], "binaries": [...]}."""
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
    _check_keys(doc, DEPS_FILE, (), ("apt", "scripts", "binaries"))

    apt = []
    for i, pkg in enumerate(_section(doc, "apt")):
        if not isinstance(pkg, str) or not APT_RE.match(pkg):
            raise DepsError(f"apt[{i}]: invalid package spec: {pkg!r}")
        apt.append(pkg)

    scripts = []
    for i, s in enumerate(_section(doc, "scripts")):
        w = f"scripts[{i}]"
        _check_keys(s, w, ("name", "url"), ("sha256", "args", "env"))
        args = s.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise DepsError(f"{w}: args must be a list of strings")
        env = s.get("env") or {}
        if not isinstance(env, dict):
            raise DepsError(f"{w}: env must be a mapping")
        for k, v in env.items():
            if not isinstance(k, str) or not ENV_RE.match(k) or ENV_DENY.match(k):
                raise DepsError(f"{w}: env name not allowed: {k!r}")
            if not isinstance(v, (str, int, float, bool)) or "\0" in str(v):
                raise DepsError(f"{w}: env {k} must be a plain scalar")
        scripts.append({"name": _str(s, "name", w, NAME_RE), "url": _url(s, w),
                        "sha256": _sha(s, w, required=False), "args": args,
                        "env": {k: str(v) for k, v in env.items()}})

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
    return {"apt": apt, "scripts": scripts, "binaries": binaries}


def summary(deps):
    """One line per non-empty section, for logs and the sandbox CLAUDE.md."""
    out = []
    if deps["apt"]:
        out.append("apt: " + ", ".join(f"`{p}`" for p in deps["apt"]))
    if deps["scripts"]:
        out.append("scripts: " + ", ".join(f"`{s['name']}`" for s in deps["scripts"]))
    if deps["binaries"]:
        out.append("binaries: " + ", ".join(
            f"`{b['name']}` (/usr/local/bin)" for b in deps["binaries"]))
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
            return tf.extractfile(m).read()
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
