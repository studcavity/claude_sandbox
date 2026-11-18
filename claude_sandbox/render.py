"""render.py - render the session CLAUDE.md and the deps reference from their templates."""

import re
import string
from claude_sandbox import sessions
from claude_sandbox.container import deps
from claude_sandbox.container import host
from claude_sandbox.container.mounts import mounts_section
from claude_sandbox.container.incus import HOME, UID, USER
from claude_sandbox.backends.github import access_lines


# The checker, copied into the sandbox so a session can lint its own commits
# before it exits (plan 4d). Stdlib-only modules; the list is tested against
# the checker's real import graph.
# Reference text the session reads on demand instead of carrying it in every
# prompt: rendered from DEPS_REF_TEMPLATE (next to this script) to DEPS_REF_PATH.
DEPS_REF_TEMPLATE = "sandbox_ref_deps.md"


DEPS_REF_PATH = "~/.claude/ref/deps.md"


def parse_probe(out):
    """Parse the key=value lines printed by the container probe."""
    facts = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    # usb= carries bare vid:pid pairs only. Strings a device reports about
    # itself (product, serial) are attacker-controlled and never read.
    usb = {host.parse_usb_spec(t) for t in facts.get("usb", "").split()
           if host.USB_SPEC_RE.match(t)}
    return {"sudo": facts.get("sudo", "").strip() == "yes",
            "tools": facts.get("tools", "").split(),
            "branch": facts.get("branch", "").strip(),
            "usb": usb}


VARIANT_RE = re.compile(r"^<!-- if ([a-z]+) -->$")


VARIANT_END = "<!-- end -->"


def select_variant(template, kind):
    """Keep the `<!-- if KIND -->` ... `<!-- end -->` blocks for this
    backend's kind and drop the others (markers on lines of their own; no
    nesting). Text outside any block is shared. ValueError if unbalanced."""
    out, keep, inside = [], True, None
    for n, line in enumerate(template.splitlines(keepends=True), 1):
        m = VARIANT_RE.match(line.strip())
        if m:
            if inside:
                raise ValueError(f"line {n}: nested `<!-- if -->`")
            inside, keep = m.group(1), m.group(1) == kind
        elif line.strip() == VARIANT_END:
            if not inside:
                raise ValueError(f"line {n}: `<!-- end -->` without `<!-- if -->`")
            inside, keep = None, True
        elif keep:
            out.append(line)
    if inside:
        raise ValueError(f"unclosed `<!-- if {inside} -->`")
    return "".join(out)


def render_claude_md(template, facts, container, workdir, repo, read_only,
                     deps_note, usb_devices=(), cli="incus",
                     disk_note="", session=None, access=None, kind="github",
                     mounts=(), declined_mounts=()):
    """Fill the sandbox CLAUDE.md template.

    The template is plain Markdown with string.Template placeholders
    ($container, $access, $user, $uid, $home, $sudo, $tools, $branch, $deps,
    $deps_file, $host_file, $usb, $mounts, $disk, $session, $prefix, $repo); write $$ for a
    literal dollar sign. `facts` comes from
    parse_probe().
    """
    branch = facts["branch"]
    return string.Template(select_variant(template, kind)).substitute(
        container=container,
        session=session or container,
        repo=repo or "(no repo)",
        prefix=sessions.branch_prefix(session or container),
        access="\n".join(access if access is not None else
                          access_lines(workdir, repo, branch, read_only)),
        user=USER, uid=UID, home=HOME,
        sudo=("Passwordless `sudo` works, so installing apt packages is "
              "fine." if facts["sudo"] else
              "No passwordless `sudo`, so you can't install system "
              "packages."),
        tools=", ".join(f"`{t}`" for t in facts["tools"]) or "(minimal image)",
        branch=f" (`{branch}`)" if branch else "",
        deps=deps_note,
        deps_file=deps.DEPS_FILE,
        host_file=host.HOST_FILE,
        disk=disk_note or "- Disk limits are unknown; check `df -h`.",
        mounts=mounts_section(mounts, cli, container, declined_mounts)
        or "No host directories are mounted.",
        usb=host.usb_section(usb_devices, cli, container,
                                     facts["usb"]) or "No USB devices were requested.",
    )


def render_deps_ref(template):
    """Fill the deps-file reference (placeholders: $deps_file, $user)."""
    return string.Template(template).substitute(
        deps_file=deps.DEPS_FILE, user=USER)
