"""
container/host.py - parse and approve a repo's claude_sandbox_host.yaml.

Runs on the HOST. This file is separate from claude_sandbox_deps.yaml on
purpose:

  * deps.yaml describes what is *installed in* the container and feeds the
    image cache; host.yaml describes what the container is *given from the
    host* at launch. Editing host.yaml can never invalidate or rebuild an
    image, because nothing here is read by container/images.py.
  * Launchers that predate this file simply never fetch it, so a repo can add
    it without breaking them (adding a key to deps.yaml would make older
    launchers reject the whole file).

Everything in the file is a REQUEST. The repo controls the file, so it can
never grant itself hardware or host files: a request is attached only if the
host user approves it (--usb flag, interactive prompt, or the host allowlist
file for USB; --mount, --allow-mounts or an interactive prompt for mounts;
--allow-files or a prompt for `files:`, host files copied into the sandbox).
Unknown keys are warnings, not errors (see deps._check_keys), so a repo can
use a newer host file with an older launcher.
"""
import os
import re
import stat

import yaml

from claude_sandbox.container.deps import (NAME_RE, DepsError, _check_keys,
                                           _section, _str, check_file_set,
                                           file_entry)

HOST_FILE = "claude_sandbox_host.yaml"
MAX_HOST_FILE = 64 * 1024
MAX_USB = 8
MAX_TOOLS = 8
MAX_MOUNTS = 8
# Host path, in-container path: plain path characters only (no `:` or `,`,
# which the --mount spec and incus options use as delimiters).
MOUNT_SOURCE_RE = re.compile(r"^(~|/)[A-Za-z0-9._/+@ -]{0,300}$")
MOUNT_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+-]{1,200}$")

HEX4_RE = re.compile(r"^[0-9a-fA-F]{4}$")
USB_SPEC_RE = re.compile(r"^([0-9a-fA-F]{4}):([0-9a-fA-F]{4})$")
# Text from the repo that ends up in a prompt and in CLAUDE.md. A conservative
# allowlist (no backticks, `$`, `#`, `<`, newlines or other markup) instead of
# escaping, so it cannot open a code fence or fake a heading.
DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9 .,()_/+:'-]{1,200}$")


def parse_usb_spec(spec):
    """'2e8a:000C' -> ('2e8a', '000c'). Full vid:pid only; no wildcards."""
    m = USB_SPEC_RE.match(spec or "")
    if not m:
        raise ValueError(f"expected VID:PID as 4+4 hex digits, got {spec!r}")
    return m.group(1).lower(), m.group(2).lower()


def parse(raw):
    """bytes/str -> {"usb": [{name, vendor_id, product_id, description, tools}],
    "mounts": [{source, path|None, mode, description}],
    "files": [pinned host files, as deps.file_entry], "warnings": [...]}"""
    if isinstance(raw, bytes):
        if len(raw) > MAX_HOST_FILE:
            raise DepsError(f"{HOST_FILE} is larger than {MAX_HOST_FILE} bytes")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise DepsError(f"{HOST_FILE} is not valid UTF-8") from None
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise DepsError(f"{HOST_FILE}: invalid YAML: {e}") from None
    doc = doc or {}
    warn: list[str] = []
    _check_keys(doc, HOST_FILE, (), ("usb", "mounts", "files"), warn)

    usb = []
    for i, d in enumerate(_section(doc, "usb")):
        w = f"usb[{i}]"
        _check_keys(d, w, ("name", "vendor_id", "product_id", "description"),
                    ("tools",), warn)
        tools = d.get("tools") or []
        if (not isinstance(tools, list) or len(tools) > MAX_TOOLS
                or not all(isinstance(t, str) and NAME_RE.match(t)
                           for t in tools)):
            raise DepsError(f"{w}: tools must be a short list of plain names")
        usb.append({
            "name": _str(d, "name", w, NAME_RE),
            "vendor_id": _str(d, "vendor_id", w, HEX4_RE).lower(),
            "product_id": _str(d, "product_id", w, HEX4_RE).lower(),
            "description": _str(d, "description", w, DESCRIPTION_RE),
            "tools": tools,
        })
    if len(usb) > MAX_USB:
        raise DepsError(f"usb: at most {MAX_USB} entries")
    for key, what in (("name", "names"),
                      (lambda u: (u["vendor_id"], u["product_id"]), "vid:pid")):
        vals = [u[key] if isinstance(key, str) else key(u) for u in usb]
        if len(set(vals)) != len(vals):
            raise DepsError(f"usb: duplicate {what}")

    mounts = []
    for i, d in enumerate(_section(doc, "mounts")):
        w = f"mounts[{i}]"
        _check_keys(d, w, ("source", "description"), ("path", "mode"), warn)
        mode = d.get("mode", "ro")
        if mode not in ("ro", "rw"):
            raise DepsError(f"{w}: mode must be ro or rw")
        mounts.append({
            "source": _str(d, "source", w, MOUNT_SOURCE_RE),
            "path": _str(d, "path", w, MOUNT_PATH_RE) if "path" in d else None,
            "mode": mode,
            "description": _str(d, "description", w, DESCRIPTION_RE),
        })
    if len(mounts) > MAX_MOUNTS:
        raise DepsError(f"mounts: at most {MAX_MOUNTS} entries")
    files = [file_entry(f, f"files[{i}]", warn, "host_path")
             for i, f in enumerate(_section(doc, "files"))]
    check_file_set(files, "files")
    return {"usb": usb, "mounts": mounts, "files": files, "warnings": warn}


# --------------------------------------------------------------------------- #
# host allowlist
# --------------------------------------------------------------------------- #
def parse_allowlist(text):
    """Lines of `vid:pid` or `vid:pid owner/repo` -> {(vid, pid, repo|None)}.

    The optional repo scopes the approval to one repository. Comments (#) and
    blank lines are ignored; a malformed line raises ValueError (fail closed
    rather than silently granting less or more than intended).
    """
    out = set()
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].split()
        if not line:
            continue
        if len(line) > 2:
            raise ValueError(f"line {n}: expected 'VID:PID [owner/repo]'")
        try:
            vid, pid = parse_usb_spec(line[0])
        except ValueError as e:
            raise ValueError(f"line {n}: {e}") from None
        out.add((vid, pid, line[1].lower() if len(line) == 2 else None))
    return out


def load_allowlist(path):
    """Read the allowlist; missing file means empty. Refuses a file that other
    users could have written (a writable allowlist is a writable grant)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return set()
    if st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"{path} must be owned by you and not group/world "
                         "writable")
    with open(path) as f:
        return parse_allowlist(f.read())


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #
def display_text(s):
    """Repo-supplied text made safe to print in a terminal prompt."""
    return "".join(c if c.isprintable() else "?" for c in s)[:200]


def resolve_usb(requests, flag_specs, allowlist, repo, ask):
    """Decide which USB devices to attach.

    requests:   parsed `usb` entries from the repo (untrusted requests)
    flag_specs: ['2e8a:000c', ...] from --usb (explicit grants)
    allowlist:  from load_allowlist
    ask:        callable(request) -> bool, or None when not interactive

    Returns a list of device dicts (name, vendor_id, product_id, description,
    tools, source, approved). Nothing is approved implicitly: a repo request
    needs an allowlist match or a yes from `ask`.
    """
    devices = {}
    for spec in flag_specs:
        vid, pid = parse_usb_spec(spec)
        devices[(vid, pid)] = {
            "name": f"usb-{vid}-{pid}", "vendor_id": vid, "product_id": pid,
            "description": "", "tools": [], "source": "flag", "approved": True}
    repo = (repo or "").lower()
    for r in requests:
        key = (r["vendor_id"], r["product_id"])
        if key in devices:  # flag wins; keep the repo's descriptive text
            devices[key].update(name=r["name"], description=r["description"],
                                tools=r["tools"])
            continue
        allowed = ((*key, None) in allowlist
                   or bool(repo and (*key, repo) in allowlist))
        devices[key] = dict(r, source="repo",
                            approved=allowed or bool(ask and ask(r)))
    return list(devices.values())


# --------------------------------------------------------------------------- #
# reporting (for the sandbox CLAUDE.md)
# --------------------------------------------------------------------------- #
def usb_state(dev, visible):
    if not dev["approved"]:
        return "requested but **not approved** by the host user"
    if (dev["vendor_id"], dev["product_id"]) in visible:
        return "attached and visible"
    return ("attached but not visible (not plugged in on the host, or in use "
            "by a process there)")


def usb_section(devices, cli, container, visible):
    """Markdown list of this launch's USB devices ('' when there are none)."""
    if not devices:
        return ""
    lines = ["Devices for this launch:"]
    for d in devices:
        vp = f"{d['vendor_id']}:{d['product_id']}"
        desc = f", {d['description']}" if d["description"] else ""
        tools = (" (tools: " + ", ".join(f"`{t}`" for t in d["tools"]) + ")"
                 if d["tools"] else "")
        lines.append(f"- `{d['name']}` {vp}{desc}{tools}: "
                     f"{usb_state(d, visible)}")
    ex = next((d for d in devices if d["approved"]), None)
    if ex:
        lines.append(
            f"Attached by the host with: `{cli} config device add {container}"
            f" usb-{ex['vendor_id']}-{ex['product_id']} usb "
            f"vendorid={ex['vendor_id']} productid={ex['product_id']} "
            "uid=1000 gid=1000 mode=0660`.")
    return "\n".join(lines)
