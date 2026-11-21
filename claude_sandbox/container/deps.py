"""
container/deps.py - parse, validate and download a repo's claude_sandbox_deps.yaml.

Runs on the HOST. Nothing here executes downloaded content; claude_sandbox/cli.py
pushes the verified bytes into the container. Design rules:

  * strict values, tolerant keys: odd names and bad URLs are errors; keys this
    launcher does not know are warnings (a newer file on an older launcher)
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
# `attest:` on a binary: the GitHub repo (owner/name) whose build attestation
# the downloaded file must carry (verified on the host with `gh`).
ATTEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
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
def _check_keys(item, where, required, optional=(), warn=None):
    """Require `required` keys; unknown keys are not an error (a newer file
    read by an older launcher) but are reported: appended to `warn`, a list of
    messages for the caller to show, and ignored. Known keys are still fully
    validated by the caller."""
    if not isinstance(item, dict):
        raise DepsError(f"{where}: expected a mapping, got {type(item).__name__}")
    extra = set(item) - set(required) - set(optional)
    missing = set(required) - set(item)
    if missing:
        raise DepsError(f"{where}: missing key(s): {', '.join(sorted(missing))}")
    if extra and warn is not None:
        names = ", ".join(sorted(_shown(k) for k in extra))
        warn.append(f"{where}: ignoring unknown key(s): {names} "
                    "(written for a newer launcher?)")


def _shown(key):
    """A repo-supplied key made safe to print: bounded, printable, quoted."""
    text = "".join(c if c.isprintable() else "?" for c in str(key))
    return repr(text[:40] + ("..." if len(text) > 40 else ""))


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


def _repo_scripts(doc, key, warn):
    """The `setup:` / `check:` section: repo scripts run in the container, as
    the session user, in the clone (never baked into an image)."""
    out = []
    for i, s in enumerate(_section(doc, key)):
        w = f"{key}[{i}]"
        _check_keys(s, w, ("name", "path"), ("args", "env", "timeout"), warn)
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


def file_entry(f, where, warn, source):
    """One pinned file entry, shared by `downloads:` here (source "url") and
    the host file's `files:` (source "host_path"). Returns the normalised dict
    (the other source key is None); files.py acts on it.

    Always required: name, dest, sha256, and the source key. dest is
    `~/<relative>` outside FILE_DEST_DENY; mode a quoted octal string;
    on_error `fail` (default) or `skip`.
    """
    _check_keys(f, where, ("name", "dest", "sha256", source),
                ("mode", "on_error"), warn)
    dest = _str(f, "dest", where, FILE_DEST_RE)
    rel = dest[2:]
    if any(seg in (".", "..") for seg in rel.split("/")):
        raise DepsError(f"{where}: invalid dest: {dest!r}")
    for bad in FILE_DEST_DENY:
        if rel == bad or rel.startswith(bad + "/"):
            raise DepsError(f"{where}: dest {dest!r} is not allowed (it is "
                            f"under ~/{bad}: shell, credential or "
                            "autostart config)")
    mode = f.get("mode", "0644")
    if not isinstance(mode, str) or not FILE_MODE_RE.match(mode):
        raise DepsError(f"{where}: mode must be a quoted octal string such as "
                        f"\"0644\", got {mode!r}")
    on_error = f.get("on_error", "fail")
    if on_error not in ON_ERROR:
        raise DepsError(f"{where}: on_error must be one of "
                        f"{', '.join(ON_ERROR)}, got {on_error!r}")
    url = host_path = None
    if source == "url":
        url = _url(f, where)
    else:
        host_path = _str(f, "host_path", where, FILE_HOST_RE)
    return {"name": _str(f, "name", where, NAME_RE), "dest": dest,
            "url": url, "host_path": host_path,
            "sha256": _sha(f, where, required=True),
            "mode": mode, "on_error": on_error}


def check_file_set(entries, section):
    """Limits and duplicates across one section's entries."""
    if len(entries) > MAX_FILES:
        raise DepsError(f"{section}: at most {MAX_FILES} entries")
    for key in ("name", "dest"):
        if len({f[key] for f in entries}) != len(entries):
            raise DepsError(f"{section}: duplicate {key}s")


def parse(raw):
    """bytes/str -> {"apt", "scripts", "binaries", "downloads", "setup", "check",
    "warnings" (unknown keys that were ignored), + metadata}.

    Unknown keys are warnings, not errors, so a file written for a newer
    launcher still works on an older one; every key we do know is validated."""
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
    warn: list[str] = []
    _check_keys(doc, DEPS_FILE, (),
                ("image", "apt", "scripts", "binaries", "downloads", "setup",
                 "check", "last_generated", "platform"), warn)

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
        _check_keys(s, w, ("name", "url"), ("sha256", "args", "env"), warn)
        scripts.append({"name": _str(s, "name", w, NAME_RE), "url": _url(s, w),
                        "sha256": _sha(s, w, required=False),
                        "args": _args(s, w), "env": _env(s, w)})

    binaries = []
    for i, b in enumerate(_section(doc, "binaries")):
        w = f"binaries[{i}]"
        _check_keys(b, w, ("name", "url", "sha256"),
                    ("archive_path", "attest"), warn)
        member = b.get("archive_path")
        if member is not None and (not isinstance(member, str) or not member
                                   or "\0" in member):
            raise DepsError(f"{w}: invalid archive_path: {member!r}")
        attest = b.get("attest")
        if attest is not None:
            attest = _str(b, "attest", w, ATTEST_RE)
        binaries.append({"name": _str(b, "name", w, NAME_RE), "url": _url(b, w),
                         "sha256": _sha(b, w, required=True),
                         "archive_path": member, "attest": attest})

    names = [b["name"] for b in binaries]
    if len(set(names)) != len(names):
        raise DepsError("binaries: duplicate names")

    downloads = [file_entry(f, f"downloads[{i}]", warn, "url")
                 for i, f in enumerate(_section(doc, "downloads"))]
    check_file_set(downloads, "downloads")
    setup = _repo_scripts(doc, "setup", warn)
    check = _repo_scripts(doc, "check", warn)
    return {"image": image, "apt": apt, "scripts": scripts,
            "binaries": binaries, "downloads": downloads,
            "files": [],   # filled by the launcher: downloads + approved host files
            "setup": setup, "check": check,
            "last_generated": last_generated, "platform": platform,
            "warnings": warn}


# prek (https://github.com/j178/prek) is installed in every sandbox, whatever
# the repo's deps file says. Pinned: bump the version and BOTH digests together
# (the .sha256 files in the release; the attestation is checked at install).
PREK_VERSION = "v0.5.11"
PREK_REPO = "j178/prek"
PREK_TARGETS = {   # platform.machine() -> (release target, sha256 of the .tar.gz)
    "x86_64": ("x86_64-unknown-linux-gnu",
               "a4ff4c70aef6f21925aa52b82d0746095fbd66893ac3b053e859f6d3454c16ae"),
    "aarch64": ("aarch64-unknown-linux-gnu",
                "f5ce760188710a34f32cf7cc52a5f2150e5d0e90189e4328243f3ff5d4037164"),
}
# prek's agent skill (one file), fetched on the host at the same tag and pinned
# by sha256, so no `gh` is needed in the container. Bump with PREK_VERSION.
PREK_SKILL_URL = (f"https://raw.githubusercontent.com/{PREK_REPO}/"
                  f"{PREK_VERSION}/skills/prek/SKILL.md")
PREK_SKILL_SHA256 = "fc06a9c64008f3c16ee2765fc79b230186b168ce1bd8d3bdd22fe040016a8a52"
PREK_SKILL_MAX = 64 * 1024
PREK_TARGETS["arm64"] = PREK_TARGETS["aarch64"]


def prek_entry(machine):
    """The `binaries:` entry for prek on this CPU, or None if there is no
    build for it. Pure."""
    target = PREK_TARGETS.get(machine)
    if target is None:
        return None
    triple, sha = target
    return {"name": "prek", "sha256": sha, "attest": PREK_REPO,
            "url": f"https://github.com/{PREK_REPO}/releases/download/"
                   f"{PREK_VERSION}/prek-{triple}.tar.gz",
            "archive_path": f"prek-{triple}/prek"}


def with_prek(deps, machine):
    """`deps` (parsed deps, or None) plus prek, unless the repo already
    installs a binary called `prek` (its choice wins). Pure."""
    deps = dict(deps) if deps else parse("")
    entry = prek_entry(machine)
    if entry and all(b["name"] != "prek" for b in deps["binaries"]):
        deps["binaries"] = list(deps["binaries"]) + [entry]
    return deps


def cache_key(deps):
    """Hex digest identifying what gets installed, or None if it must not be
    baked into a cached image.

    Only the install sections feed the key (comments, ordering of keys, the
    metadata fields, `setup`/`check` (run in the container, never baked) and `image` don't;
    the image is already part of the deps image's alias via the base image).
    Scripts without a sha256 are whatever the URL serves at build time, and a
    cached image would freeze that with no way to notice, so such deps are
    never cached.

    `deps["files"]` (set by the launcher) are the pinned files baked in:
    downloads plus the host files the user approved. They are keyed by what
    ends up on disk (dest, mode, sha256), not by where the bytes came from, so
    approving a host file, or a new pin, builds a new image and an unapproved
    launch can never reuse one that holds a file it did not approve.
    """
    files = [{"dest": f["dest"], "mode": f["mode"], "sha256": f["sha256"]}
             for f in sorted(deps.get("files") or [], key=lambda f: f["dest"])]
    if not (deps["apt"] or deps["scripts"] or deps["binaries"] or files):
        return None
    if any(s["sha256"] is None for s in deps["scripts"]):
        return None
    parts = {k: deps[k] for k in ("apt", "scripts", "binaries")}
    if files:   # absent when empty, so keys of file-less deps never change
        parts["files"] = files
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"))
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
    if deps.get("downloads"):
        out.append("downloads (sha256-pinned files under your home): "
                   + ", ".join(f"`{f['dest']}`" for f in deps["downloads"]))
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
