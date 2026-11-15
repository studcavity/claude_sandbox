#!/usr/bin/env python3
"""
claude_sandbox.py - throwaway LXD/Incus container for a Claude Code session.

What it does:
  1. Launches an ephemeral container.
  2. Generates an SSH key *inside* the container. The private key never
     touches the host.
  3. Registers the public key with GitHub, either as a deploy key on one
     repo (recommended) or, only with --account-key, as an account key.
  4. Installs Claude Code and drops you into it (or a shell with --shell).
     Packages and Claude Code are baked into a cached local image
     (claude-sandbox-<hash>) on first run, so later launches skip the
     install. The image is rebuilt when the recipe or --image changes, when
     it is older than --max-age days, or on --rebuild. --no-cache disables it.
     A repo's claude_sandbox_deps.yaml is baked into a second per-repo image
     (claude-sandboxdeps-<hash>) the same way, unless it has an unpinned
     install script; see README.md.
  5. On exit (normal, error, Ctrl-C, SIGTERM, SIGHUP) deletes the GitHub key
     and destroys the container.

GitHub auth: all GitHub calls go through the host's `gh` CLI (run locally with
invoke), so log in with `gh auth login` (or set GH_TOKEN / GITHUB_TOKEN for gh).
No token is handled by this program. The launcher refuses to run when gh is
logged in as anyone but --github-user (override: --no-user-check).
  - Keys inherit the token's scopes when pushing: without `workflow` the
    sandbox can't push .github/workflows/ changes. Keep it that way: a pushed
    workflow runs with the repo's Actions secrets before any review. The
    launcher warns at startup when the token has it.
  - Account key mode needs the `admin:public_key` scope
    (gh auth refresh -s admin:public_key).
  - Deploy key mode needs admin rights on the repo
    (fine-grained token: "Administration: read & write").

Claude auth, first match wins:
  - CLAUDE_CODE_OAUTH_TOKEN on the host, or the token file
    (~/.config/claude_sandbox/oauth_token; create it with --setup-token,
    which runs `claude setup-token` in a throwaway sandbox). Long-lived and
    read-only, so it is safe to share between concurrent sessions.
  - ANTHROPIC_API_KEY on the host (API billing).
  - Otherwise, run /login inside Claude Code.
Secrets are piped into the container over stdin, never put on a command line.

Before launching, it checks whether this checkout is behind its upstream and,
on a terminal, offers to `git pull` (skip with --no-update-check). On first run
it creates ./venv from requirements.txt and re-runs itself inside it.

Examples:
  ./claude_sandbox.py myproject                    # deploy key + clone of <github-user>/myproject
  ./claude_sandbox.py other-user/proj              # a repo under another owner
  ./claude_sandbox.py proj --model opus            # pick the session model
  ./claude_sandbox.py --local ~/src/proj           # a git repo on this machine, no GitHub
  ./claude_sandbox.py --account-key                # no repo: account-wide key
  ./claude_sandbox.py --setup-token                # one-time Claude login token
"""
import argparse
import collections
import getpass
import os
import re
import shlex
import string
import threading
import subprocess
import signal
import sys

if __name__ == "__main__":
    # Standard library only, before the third-party imports below: ensures a
    # venv with requirements.txt and re-execs into it. Skipped on import (tests).
    import sandbox_bootstrap
    sandbox_bootstrap.ensure_venv(os.path.dirname(os.path.abspath(__file__)))

import sandbox_claude_config
import sandbox_deps
import sandbox_host
import sandbox_pool
import sandbox_volumes
import sandbox_provenance
import sandbox_sessions
import sandbox_timing
import sandbox_update
from sandbox_containers import HOME, UID, USER, Container
import sandbox_backend_github
import sandbox_backend_local
import sandbox_github
from sandbox_backend_github import GitHubBackend, access_lines
from sandbox_backend_local import LocalBackend, resolve_local
from sandbox_github import GitHubError, resolve_repo, validate_repo
from sandbox_images import apply_deps, ensure_base_image, ensure_deps_image
from sandbox_util import log, sigint_to_child

# `incus/lxc exec` starts with a clean environment: only TERM is carried over
# from the client. Without the rest, Claude Code can't identify the outer
# terminal (or that it's running under tmux/screen) and falls back to
# conservative rendering, which can break native scrollback. Forward the
# terminal-identity variables so the session behaves like it does on the host.
# (LANG/LC_ALL are deliberately not forwarded: the host locale may not exist
# in the container; incus already sets LANG=C.UTF-8.)
TERM_ENV_VARS = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
                 "LC_TERMINAL", "LC_TERMINAL_VERSION")

# Defaults for the maintainer's setup; each can be overridden by a flag or the
# environment variable noted in --help.
DEFAULT_GITHUB_USER = "studcavity"
DEFAULT_GIT_EMAIL = "353176155+studcavity@users.noreply.github.com"
DEFAULT_IMAGE = "images:ubuntu/26.04"  # Ubuntu 26.04 LTS
# Storage quotas (incus sizes). The root disk was 10GiB by default, which a
# deps install or a nested incus overran. Quotas are caps, not allocations, so
# these cost nothing until used, and they bound what one session can take from
# the host's pool. 0 disables.
DEFAULT_DISK_SIZE = "30GiB"
DEFAULT_VOLUME_SIZE = "20GiB"
DEFAULT_CLI = "incus"

DEFAULT_TOKEN_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "claude_sandbox", "oauth_token")
DEFAULT_USB_ALLOW = os.path.join(os.path.dirname(DEFAULT_TOKEN_FILE),
                                 "usb_allow")
SANDBOX_MARKER = "/etc/claude-sandbox"  # scripts test for this to gate hazards
SECRETS_FILE = f"{HOME}/.sandbox_secrets"  # inside the container

# Tools listed in the sandbox's CLAUDE.md when present.
SANDBOX_TOOLS = ("git", "python3", "pip3", "rg", "fd", "jq", "tree", "gcc",
                 "make", "gdb", "strace", "lsof", "htop", "dig", "nc", "tmux",
                 "curl", "wget")

# The checker, copied into the sandbox so a session can lint its own commits
# before it exits (plan 4d). Stdlib-only modules; the list is tested against
# the checker's real import graph.
# Reference text the session reads on demand instead of carrying it in every
# prompt: rendered from DEPS_REF_TEMPLATE (next to this script) to DEPS_REF_PATH.
DEPS_REF_TEMPLATE = "sandbox_ref_deps.md"
DEPS_REF_PATH = "~/.claude/ref/deps.md"
SESSION_TOOL_DIR = f"{HOME}/.local/share/claude-sandbox"
SESSION_TOOL_BIN = f"{HOME}/.local/bin/sandbox-check"
SESSION_TOOL_MODULES = ("sandbox_check", "sandbox_todo", "sandbox_untrusted")
SESSION_TOOL_WRAPPER = """#!/bin/sh
# Installed by claude_sandbox. Lints this session's own commits (everything
# since the remote's default branch) with the same rules CI and the launcher
# use. With arguments it is just sandbox_check.py.
set -eu
tool="${HOME}/.local/share/claude-sandbox/sandbox_check.py"
if [ "$#" -gt 0 ]; then exec python3 "$tool" "$@"; fi
# Best effort: without a fresh origin the "behind" check would be stale.
timeout 30 git fetch -q origin || echo "sandbox-check: fetch failed; behind-check may be stale" >&2
base=$(git rev-parse --verify -q origin/HEAD || git rev-parse --verify -q origin/main \
       || git rev-parse --verify -q origin/master) || {
  echo "sandbox-check: can't find origin's default branch" >&2; exit 2; }
exec python3 "$tool" --fresh "$base" HEAD
"""


def marker_script(name, path=None):
    """Shell that writes the sandbox marker file (repo scripts test for it to
    refuse to run anywhere else). `name` is quoted; the path is ours."""
    path = path or SANDBOX_MARKER
    return (f"printf '%s\\n' {shlex.quote(name)} > {shlex.quote(path)} && "
            f"chmod 0644 {shlex.quote(path)}")


def git_config_script(git_name=None, git_email=None, github_ssh=True):
    """The `git config --global` calls a session needs, as one shell script:
    https->ssh rewrite for GitHub (the deploy key is ssh; skipped when there is
    no key, so public https clones still work), optional identity, and the
    linear-history settings. Values are quoted."""
    cmds = (['git config --global url."git@github.com:".insteadOf "https://github.com/"']
            if github_ssh else [])
    if git_name:
        cmds.append("git config --global user.name " + shlex.quote(git_name))
    if git_email:
        cmds.append("git config --global user.email " + shlex.quote(git_email))
    cmds += ["git config --global pull.rebase true",       # linear history:
             "git config --global rebase.autoStash true",  # rebase on pull,
             "git config --global merge.ff only",          # no merge commits,
             "git config --global push.autoSetupRemote true"]
    return " && ".join(cmds)


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
    files = []
    for name in SESSION_TOOL_MODULES:
        with open(os.path.join(src_dir, name + ".py"), "rb") as f:
            files.append((f"{SESSION_TOOL_DIR}/{name}.py", f.read(), "0644"))
    files.append((SESSION_TOOL_BIN, SESSION_TOOL_WRAPPER.encode(), "0755"))
    return files


DEFAULT_MODEL = "claude-sonnet-5-5"
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"

TMUX_CONF = """\
set -g mouse on
set -g history-limit 100000
set -g default-terminal tmux-256color
set -as terminal-features ',*:RGB'
set -g status off
"""


# --------------------------------------------------------------------------- #
# pure helpers (no I/O; unit-tested)
# --------------------------------------------------------------------------- #
def read_token_file(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_token_file(path, token):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, path)



def claude_secrets(environ, token_file):
    """Claude credentials to pass into the sandbox, from env or token file."""
    secrets = {}
    token = environ.get("CLAUDE_CODE_OAUTH_TOKEN") or read_token_file(token_file)
    if token:
        secrets["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if environ.get("ANTHROPIC_API_KEY"):
        secrets["ANTHROPIC_API_KEY"] = environ["ANTHROPIC_API_KEY"]
    return secrets


def secrets_script(secrets):
    """Shell `export` lines for secrets (sourced in the container)."""
    return "".join(f"export {k}={shlex.quote(v)}\n" for k, v in secrets.items())


def session_script(workdir, load_secrets, shell=False, model="default",
                   tmux=False, resume_prompt=None):
    """The bash -lc script that starts the interactive session. With
    `resume_prompt`, it continues the container's last conversation (the only
    one there is) and opens with that prompt."""
    prog = "bash -l" if shell else "claude"
    if not shell and model.lower() != "default":
        prog += f" --model {shlex.quote(model)}"
    if not shell and resume_prompt is not None:
        prog += f" --continue {shlex.quote(resume_prompt)}"
    if tmux:
        # tmux sets its own TERM/TERM_PROGRAM, giving Claude Code a
        # well-known terminal; scroll with the mouse or prefix-[ .
        prog = f"tmux new-session -s claude {shlex.quote(prog)}"
    return f"{load_secrets}cd {shlex.quote(workdir)} && exec {prog}"


def setup_argv(s):
    """argv for one validated `setup:` entry: bounded by `timeout` (killed
    10s later if it ignores SIGTERM), run as a login shell script so the
    session's PATH applies. Nothing is interpolated into a shell string."""
    return ["timeout", "--kill-after=10", str(s["timeout"]),
            "bash", "-l", "./" + s["path"], *s["args"]]


# The final check (`check:` in the deps file): see Sandbox.final_check.
CHECK_OUTPUT_CHARS = 6000   # per failing script: what the resumed session is shown
_CONTROL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")


def tail_text(text, limit=CHECK_OUTPUT_CHARS):
    """The last `limit` characters of `text`, minus terminal escapes and
    control characters (a test run's colours and progress bars), with a marker
    when something was cut."""
    text = _CONTROL_RE.sub("", text.replace("\r\n", "\n")).strip("\n")
    if len(text) <= limit:
        return text
    return "[... output cut ...]\n" + text[-limit:]


def check_failure_text(returncode):
    """Why one check script failed."""
    return ("timed out" if returncode == 124
            else f"exited with status {returncode}")


def resume_prompt(failures):
    """The prompt that reopens the session after a failed final check.

    `failures`: [(check entry, returncode, output)], in run order. The text
    is the project's own test output, handed back to the session that wrote
    the code, in the container it ran in.
    """
    parts = ["The launcher's final check ran after you exited, and it FAILED. "
             "The container and your push access are still alive, but only "
             "until you exit again. Fix the failures below, re-run each "
             "failing script until it passes, then commit and push (new "
             "commits; never bypass or weaken the check) and stop.\n"]
    for s, code, out in failures:
        parts.append(f"## `{s['path']}` ({s['name']}): "
                     f"{check_failure_text(code)}\n"
                     f"Run it from the repo root: `./{s['path']}`\n"
                     f"```\n{tail_text(out)}\n```\n")
    return "\n".join(parts)


def run_streaming(cmd, limit=CHECK_OUTPUT_CHARS * 2, echo=None):
    """Run `cmd` with no stdin, echoing its combined output as it arrives and
    keeping the last `limit` characters. Returns (returncode, output)."""
    echo = echo or sys.stderr
    kept: collections.deque = collections.deque()
    size = 0
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace") as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            echo.write(line)
            echo.flush()
            kept.append(line)
            size += len(line)
            while size > limit and len(kept) > 1:
                size -= len(kept.popleft())
        return proc.wait(), "".join(kept)


def ask_resume(names):
    """Interactive yes/no (default yes) to resume Claude on a failed final
    check; no answer (EOF) or no terminal is no."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"\nFinal check failed ({', '.join(names)}). Resume the "
                     "Claude session to fix it? [Y/n] "
                     ).strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


def choose_image(flag, repo_image):
    """(image, origin) for the base OS: --image beats the repo's `image:` in
    claude_sandbox_deps.yaml, which beats the default."""
    if flag:
        return flag, "--image"
    if repo_image:
        return repo_image, sandbox_deps.DEPS_FILE
    return DEFAULT_IMAGE, "default"


def parse_probe(out):
    """Parse the key=value lines printed by the container probe."""
    facts = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    # usb= carries bare vid:pid pairs only. Strings a device reports about
    # itself (product, serial) are attacker-controlled and never read.
    usb = {sandbox_host.parse_usb_spec(t) for t in facts.get("usb", "").split()
           if sandbox_host.USB_SPEC_RE.match(t)}
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
                     disk_note="", session=None, access=None, kind="github"):
    """Fill the sandbox CLAUDE.md template.

    The template is plain Markdown with string.Template placeholders
    ($container, $access, $user, $uid, $home, $sudo, $tools, $branch, $deps,
    $deps_file, $host_file, $usb, $disk, $session, $prefix, $repo); write $$ for a
    literal dollar sign. `facts` comes from
    parse_probe().
    """
    branch = facts["branch"]
    return string.Template(select_variant(template, kind)).substitute(
        container=container,
        session=session or container,
        repo=repo or "(no repo)",
        prefix=sandbox_sessions.branch_prefix(session or container),
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
        deps_file=sandbox_deps.DEPS_FILE,
        host_file=sandbox_host.HOST_FILE,
        disk=disk_note or "- Disk limits are unknown; check `df -h`.",
        usb=sandbox_host.usb_section(usb_devices, cli, container,
                                     facts["usb"]) or "No USB devices were requested.",
    )

def render_deps_ref(template):
    """Fill the deps-file reference (placeholders: $deps_file, $user)."""
    return string.Template(template).substitute(
        deps_file=sandbox_deps.DEPS_FILE, user=USER)










def usb_spec(text):
    """argparse type for --usb: normalised (vid, pid) text 'vid:pid'."""
    try:
        return "%s:%s" % sandbox_host.parse_usb_spec(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def ask_usb(req):
    """Interactive approval of one repo-requested device (default: no)."""
    print(f"\nThe repo requests USB device "
          f"\033[1m{req['vendor_id']}:{req['product_id']}\033[0m "
          f"(name {req['name']!r}).\nIts description (from the repo, "
          f"untrusted): {sandbox_host.display_text(req['description'])}\n"
          "Attach it to the sandbox? Only approve hardware you own and trust.",
          file=sys.stderr)
    try:
        return input("Attach? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False










def refresh_argv(args, python, script):
    """argv of the detached process that refreshes stale cached images: the
    same image-relevant options, so it works out the same aliases."""
    cmd = [python, script, "--refresh-images", "--cli", args.cli,
           "--max-age", str(args.max_age),
           "--hard-max-age", str(args.hard_max_age)]
    if args.repo:
        cmd += ["--repo", args.repo]
    if getattr(args, "local", None):
        cmd += ["--local", args.local]
        if args.base:
            cmd += ["--base", args.base]
    if args.image:
        cmd += ["--image", args.image]
    if args.disk_size:
        cmd += ["--disk-size", str(args.disk_size)]
    if args.no_deps:
        cmd.append("--no-deps")
    return cmd


def recovery_image(args):
    """The image for --recover's helper container: the cached base image
    (it has git), built if needed."""
    return ensure_base_image(args.cli, args.image or DEFAULT_IMAGE,
                             args.max_age, False, args.disk_size,
                             hard_max_age_days=args.hard_max_age)


def refresh_log_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "claude_sandbox", "image-refresh.log")


def spawn_refresh(args):
    """Start the refresher detached (its own session, output to a log) so the
    launcher neither waits for it nor is held open by it."""
    path = refresh_log_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a") as logf:
            subprocess.Popen(
                refresh_argv(args, sys.executable, os.path.abspath(__file__)),
                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True)
    except OSError as e:
        log(f"warning: couldn't start the background image refresh: {e}")
        return
    log(f"refreshing stale cached images in the background (log: {path})")


def refresh_images(args):
    """Rebuild whatever cached image is stale for this repo's settings, then
    exit. Safe to run twice at once: each rebuild re-checks under the lock."""
    box = Sandbox(args)
    try:
        box.load_deps()
        source = box.choose_image()
        image = ensure_base_image(args.cli, source, args.max_age, False,
                                  args.disk_size, hard_max_age_days=args.hard_max_age,
                                  refresh=True)
        if box.deps:
            ensure_deps_image(args.cli, image, box.deps, args.max_age, False,
                              args.disk_size, hard_max_age_days=args.hard_max_age,
                              refresh=True)
    except (GitHubError, subprocess.CalledProcessError, RuntimeError, OSError) as e:
        log(f"error: image refresh failed: {e}")
        return 1
    log("image refresh done")
    return 0








# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
# Run as root after deps install. Incus's default subuid range
# (1000000:1000000000) can't fit inside this container's own id map, so use a
# small one; the socket is world-accessible because the sandbox is throwaway
# and `ubuntu` already has passwordless sudo.
NESTING_SETUP = r"""
command -v incus >/dev/null || exit 0
sed -i '/^root:/d' /etc/subuid /etc/subgid
echo root:100000:65536 | tee -a /etc/subuid >> /etc/subgid
mkdir -p /etc/systemd/system/incus.socket.d
printf '[Socket]\nSocketMode=0666\n' > /etc/systemd/system/incus.socket.d/mode.conf
systemctl daemon-reload
systemctl restart incus.socket incus
incus admin init --minimal
"""


def launch_config(args):
    """Instance config for `launch`. --nesting allows containers inside."""
    return {"security.nesting": "true"} if args.nesting else {}


def size_arg(text):
    """argparse type for sizes: an incus size like 30GiB, or 0 to disable.
    Returns None for 0 so callers can treat it as "no quota"."""
    if text.strip() == "0":
        return None
    if not re.fullmatch(r"[1-9][0-9]*(\.[0-9]+)?(B|[kMGTPE]i?B?)", text.strip()):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a size like 30GiB (or 0 for no limit)")
    return text.strip()




def make_backend(args):
    """The Backend for these arguments (see sandbox_backend)."""
    if getattr(args, "local", None):
        return LocalBackend(args)
    return GitHubBackend(args)


class Sandbox:
    def __init__(self, args, backend=None):
        self.args = args
        self.cli = args.cli
        self.backend = backend or make_backend(args)
        # The container name doubles as the session id (key title, branches).
        # Never reuse a name whose signing key is recorded for this repo: the
        # new key would vouch for the old session's branches.
        self.name = args.name or sandbox_sessions.pick_name(
            Container.list_names(self.cli)
            | sandbox_provenance.recorded_sessions(self.backend.key_repo,
                                                   self.backend.key_root))
        self.session_id = sandbox_sessions.validate_session(self.name)
        self.box = Container(self.cli, self.name)
        self.container_up = False
        self._torn_down = False
        self._volumes_thread = None   # see teardown() / wait_volumes()
        self._release_kept = False    # land() said a kept volume's work is safe
        self.deps = None
        self.deps_baked = False
        self.image = None
        self.usb_devices = []
        self.volumes = sandbox_volumes.Volumes(self.cli, self.box, [], None)
        self.deps_note = ("No repo was cloned, so there is no "
                          f"`{sandbox_deps.DEPS_FILE}`.")

    def launch(self, image):
        log(f"launching ephemeral container {self.name} ({image})")
        with sandbox_timing.stage("incus launch"):
            self.box.launch(image, ephemeral=True, config=launch_config(self.args),
                            root_size=self.args.disk_size)
        self.container_up = True
        with sandbox_timing.stage("wait for network"):
            self.box.wait_network()
        with sandbox_timing.stage("self-check + marker + package probe"):
            self.box.self_check()  # fail fast, before any real work
            # The marker lets repo scripts refuse to run anywhere but inside a
            # sandbox; the package probe is a no-op from the cached image.
            # One exec for both.
            self.box.root(marker_script(self.name) + "; "
                          "command -v git >/dev/null && command -v curl >/dev/null || "
                          "(apt-get update -qq && "
                          "apt-get install -y -qq git curl openssh-client)",
                          capture=True)

        if self.args.tmux:
            self.box.root("command -v tmux >/dev/null || "
                          "(apt-get update -qq && apt-get install -y -qq tmux)",
                          capture=True)
            self.box.user("cat > ~/.tmux.conf", input=TMUX_CONF)

    def attach_volumes(self):
        """Mount size-capped volumes over /tmp and the clone directory."""
        mounts = sandbox_volumes.plan(self.name, self.backend.workdir(),
                                      self.args.volume_size,
                                      keep_work=self.backend.keep_work(
                                          self.session_id))
        self.volumes = sandbox_volumes.Volumes(
            self.cli, self.box, mounts, self.args.volume_size)
        self.volumes.attach()
        self.backend.work_kept = bool(self.volumes.kept())

    def disk_note(self):
        """Facts about this session's disks for the sandbox CLAUDE.md."""
        a = self.args
        root = (f"The root disk is capped at **{a.disk_size}**" if a.disk_size
                else "The root disk has no launcher-imposed cap")
        mounted = {m.path: m for m in self.volumes.mounts
                   if m.volume in self.volumes.created}
        vols = ", ".join(f"`{p}`" for p in mounted)
        return (f"- {root}. " + (
            f"{a.volume_size} volumes are mounted separately at {vols}, so "
            "filling one does not break the others or the OS."
            if mounted else "Everything shares that one disk."))

    def setup_nesting(self):
        """With --nesting, make a preinstalled incus usable inside the sandbox."""
        if self.args.nesting:
            log("configuring nested incus (skipped if incus isn't installed)")
            self.box.root(NESTING_SETUP, capture=True)

    def load_deps(self):
        """Fetch and validate the repo's claude_sandbox_deps.yaml (host only).

        Returns the parsed deps, or None when there is nothing to install.
        Any invalid file aborts the launch (fail closed); --no-deps skips it.
        """
        f = sandbox_deps.DEPS_FILE
        self.deps = None
        if not self.backend.has_repo:
            return None
        if self.args.no_deps:
            self.deps_note = f"Skipped (`--no-deps`): `{f}` was not applied."
            return None
        raw = self.backend.read_file(f)
        if raw is None:
            self.deps_note = f"This repo has no `{f}`, so nothing extra was installed."
            return None
        deps = sandbox_deps.parse(raw)
        lines = sandbox_deps.summary(deps)
        self.deps_note = (f"Installed from `{f}`:\n"
                          + "\n".join(f"  - {l}" for l in lines)
                          if lines else f"`{f}` is present but empty.")
        for l in lines:
            log(f"deps: {l}")
        self.deps = deps if lines else None
        return self.deps

    def choose_image(self):
        """Pick the base image once the deps file (if any) has been read, and
        tell the session where it came from when that wasn't the default."""
        wanted = (self.deps or {}).get("image")
        self.image, origin = choose_image(self.args.image, wanted)
        log(f"base image: {self.image} ({origin})")
        if wanted and origin == "--image" and wanted != self.image:
            self.deps_note += (f"\n`image: {wanted}` in the file was "
                               f"overridden by the launcher flag "
                               f"`--image {self.image}`.")
        return self.image

    def resolve_usb(self):
        """Decide which USB devices to attach (host only, before any build).

        Requests come from the repo's claude_sandbox_host.yaml; grants come
        only from --usb, the host allowlist or an interactive yes. The host
        file is separate from the deps file, so it never affects image caches.
        """
        a = self.args
        self.usb_devices = []
        if a.no_usb:
            return []
        requests = []
        raw = self.backend.read_file(sandbox_host.HOST_FILE)
        if raw is not None:
            requests = sandbox_host.parse(raw)["usb"]
        try:
            allow = sandbox_host.load_allowlist(a.usb_allow)
        except ValueError as e:
            raise RuntimeError(f"USB allowlist: {e}") from None
        self.usb_devices = sandbox_host.resolve_usb(
            requests, a.usb, allow, self.backend.key_repo,
            ask=ask_usb if sys.stdin.isatty() else None)
        for d in self.usb_devices:
            log(f"usb: {d['vendor_id']}:{d['product_id']} "
                f"{'will be attached' if d['approved'] else 'NOT attached (not approved)'}"
                f" ({d['source']})")
        return self.usb_devices

    def attach_usb(self):
        """Attach approved devices. Runs after install_deps so repo-supplied
        installers never run with hardware present, and before any credential
        exists in the container."""
        for d in self.usb_devices:
            if d["approved"]:
                self.box.add_usb(d["vendor_id"], d["product_id"])

    def install_deps(self):
        """Install the deps into this container unless the image has them.

        Runs before the SSH key and Claude credentials exist in the container,
        so installers never see them.
        """
        if self.deps and not self.deps_baked:
            apply_deps(self.box, self.deps)

    def run_setup(self, workdir):
        """Run the repo's `setup:` scripts in the container (as the session
        user, in the clone, each launch).

        Called after the clone and before Claude credentials are pushed, so
        the scripts never see them; the repo's own deploy key is present. A
        failing script never aborts the launch: it is logged and reported in
        the sandbox CLAUDE.md so the session can tell the user.
        """
        if not self.deps or not self.deps.get("setup"):
            return
        notes = []
        for s in self.deps["setup"]:
            log(f"setup: running {s['path']} (timeout {s['timeout']}s)")
            r = self.box.user_argv(setup_argv(s), env=s["env"], cwd=workdir,
                                   check=False, input="")  # never waits on a prompt
            if r.returncode == 0:
                notes.append(f"`{s['path']}`: succeeded")
            else:
                why = ("timed out" if r.returncode == 124
                       else f"exited with status {r.returncode}")
                log(f"warning: setup script {s['path']} {why}; continuing")
                notes.append(f"`{s['path']}`: **FAILED** ({why}); re-run it by "
                             "hand and tell the user")
        self.deps_note += "\nSetup results this launch:\n" + "\n".join(
            f"  - {n}" for n in notes)

    def provision_key(self):
        """The backend's network credential (if any), then known_hosts, every
        git setting and, where there are commits to sign, the session's
        signing key: one exec."""
        known = self.backend.provision(self.box, self.name, self.session_id)
        signing = self.backend.has_commits
        r = self.box.user(
            ("mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
             "cat >> ~/.ssh/known_hosts && " if known else "")
            # known_hosts is GitHub's: only then is there an SSH key to use
            + git_config_script(self.args.git_name, self.args.git_email,
                                github_ssh=bool(known))
            + (" && " + sandbox_provenance.container_script() if signing else ""),
            input=known or None, capture=signing)
        if signing:
            self.record_signing_key(r.stdout)

    def record_signing_key(self, out):
        """Record the public half of the per-session commit-signing key (made
        in the container; the private half never leaves it) on the host, so
        the launcher can later prove which commits this session made. Never
        blocks the launch: without a recorded key its PRs fail the
        provenance check (closed)."""
        try:
            if sandbox_provenance.save_key(self.backend.key_repo, self.session_id,
                                           (out.strip().splitlines() or [""])[0],
                                           root=self.backend.key_root):
                log(f"warning: replaced the recorded signing key of an earlier "
                    f"session named {self.session_id}; its unmerged PRs will "
                    "fail the provenance check")
        except (OSError, ValueError) as e:
            log(f"warning: couldn't record the session's signing key ({e}); "
                f"its PRs will fail {sandbox_provenance.CONTEXT}")

    def install_claude(self):
        if self.box.user("command -v claude >/dev/null", check=False).returncode == 0:
            return  # baked into the cached image
        log("installing Claude Code")
        self.box.user(CLAUDE_INSTALL, capture=True)

    def clone(self):
        """Put the code in the container (the backend decides how)."""
        return self.backend.populate(self.box, self.session_id)

    def install_session_tools(self):
        """Put the checker in the sandbox so the session can lint its own
        commits (`sandbox-check`). Only where there are commits to lint."""
        if not self.backend.has_commits:
            return
        # The directories are made as the session user (so they stay theirs);
        # the files then arrive as root in one tar stream: 2 execs, not 9.
        self.box.user("mkdir -p " + shlex.quote(SESSION_TOOL_DIR) + " "
                      + shlex.quote(os.path.dirname(SESSION_TOOL_BIN)))
        self.box.root_argv(["tar", "-xzf", "-", "-C", "/"],
                           input=session_tool_archive(session_tool_files(
                               os.path.dirname(os.path.abspath(__file__)))))

    def write_claude_md(self, workdir):
        """Render the --claude-md template into ~/.claude/ in the container."""
        probe = self.box.user(
            "sudo -n true 2>/dev/null && echo sudo=yes || echo sudo=no; "
            "printf 'usb='; for d in /sys/bus/usb/devices/*; do "
            "[ -r $d/idVendor ] && [ -r $d/idProduct ] && "
            "printf '%s:%s ' $(cat $d/idVendor) $(cat $d/idProduct); done; echo; "
            "printf 'tools='; for t in " + " ".join(SANDBOX_TOOLS) + "; do "
            "command -v $t >/dev/null && printf '%s ' $t; done; echo; "
            f"printf 'branch='; git -C {shlex.quote(workdir)} "
            "rev-parse --abbrev-ref HEAD 2>/dev/null || echo",
            capture=True, check=False).stdout
        with open(self.args.claude_md) as f:
            template = f.read()
        md = render_claude_md(template, parse_probe(probe),
                              container=self.name, workdir=workdir,
                              repo=self.backend.label,
                              read_only=self.args.read_only,
                              access=self.backend.access_lines(
                                  workdir, parse_probe(probe)["branch"]),
                              kind=self.backend.kind,
                              deps_note=self.deps_note,
                              session=self.session_id,
                              usb_devices=self.usb_devices, cli=self.cli,
                              disk_note=self.disk_note())
        self.box.user("mkdir -p ~/.claude/ref && cat > ~/.claude/CLAUDE.md",
                      input=md)
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, DEPS_REF_TEMPLATE)) as f:
            ref = render_deps_ref(f.read())
        self.box.user(f"cat > {DEPS_REF_PATH}", input=ref)

    def seed_claude(self, workdir, secrets):
        """Pre-answer Claude Code's first-run dialogs (see
        sandbox_claude_config). Never blocks the launch."""
        state = sandbox_claude_config.claude_state(
            workdir, secrets, self.backend.trusted())
        r = self.box.user_argv(["python3", "-c",
                                sandbox_claude_config.MERGE_SCRIPT],
                               input=sandbox_claude_config.merge_input(state),
                               capture=True, check=False)
        if r.returncode != 0:
            log("warning: couldn't pre-answer Claude Code's first-run "
                f"dialogs: {(r.stderr or '').strip()[-200:]}")

    def push_secrets(self, workdir):
        """Write credentials to a 0600 file in the container via stdin.

        `incus exec --env K=V` would put them on the host's process list.
        Returns a shell prefix that loads and removes the file.
        """
        secrets = claude_secrets(os.environ, self.args.token_file)
        self.seed_claude(workdir, secrets)
        if not secrets:
            log("no Claude credential found; run /login in the session "
                "(or create one with --setup-token)")
            return ""
        log("passing " + " and ".join(secrets) + " into the sandbox")
        self.box.user(f"umask 077 && cat > {SECRETS_FILE}",
                      input=secrets_script(secrets))
        return f". {SECRETS_FILE} && rm -f {SECRETS_FILE} && "

    def session(self, workdir):
        env = {k: os.environ[k] for k in TERM_ENV_VARS if os.environ.get(k)}
        prompt = None
        while True:
            script = session_script(workdir, self.push_secrets(workdir),
                                    shell=self.args.shell,
                                    model=self.args.model,
                                    tmux=self.args.tmux, resume_prompt=prompt)
            cmd = self.box.user_cmd("bash", "-lc", script, env=env)
            cmd[cmd.index("--cwd") + 1] = workdir

            log("starting session (exit Claude Code to tear everything down)"
                if prompt is None else
                "resuming the session with the final check's failures")
            sandbox_timing.mark("prompt")
            with sigint_to_child():
                subprocess.run(cmd)
            prompt = self.final_check(workdir)
            if prompt is None:
                return

    def final_check(self, workdir):
        """Run the repo's `check:` scripts now that Claude has exited, while
        the container and its deploy key are still alive. Returns the prompt
        to resume Claude with when something failed and the user wants that,
        else None. Ctrl-C skips the check (and tears down)."""
        a = self.args
        checks = (self.deps or {}).get("check")
        if not checks or a.shell or a.no_final_check:
            return None
        failures = []
        for s in checks:
            log(f"final check: running {s['path']} (timeout {s['timeout']}s; "
                "Ctrl-C skips it and tears down)")
            cmd = self.box.user_cmd(*setup_argv(s), env=s["env"])
            cmd[cmd.index("--cwd") + 1] = workdir
            code, out = run_streaming(cmd)
            if code != 0:
                failures.append((s, code, out))
        if not failures:
            log("final check passed")
            return None
        names = [s["path"] for s, _, _ in failures]
        log(f"final check FAILED: {', '.join(names)}")
        if not ask_resume(names):
            log("not resuming: tearing down with the check failing (what "
                "was pushed still becomes a PR; say so in review)")
            return None
        return resume_prompt(failures)

    def setup_token(self):
        """Run `claude setup-token` interactively, then save the pasted token."""
        env = {k: os.environ[k] for k in TERM_ENV_VARS if os.environ.get(k)}
        log("running `claude setup-token`: sign in in your browser, then "
            "copy the token it prints")
        with sigint_to_child():
            subprocess.run(self.box.user_cmd("bash", "-lc",
                                             "claude setup-token", env=env))
        token = getpass.getpass(
            "\nPaste the token (input hidden, empty to cancel): ").strip()
        if not token:
            log("no token entered; nothing saved")
            return 1
        if not token.startswith("sk-ant-"):
            log("warning: that doesn't look like a Claude token; saving anyway")
        save_token_file(self.args.token_file, token)
        log(f"saved token to {self.args.token_file} (mode 600)")
        return 0

    def report_host_warnings(self):
        """Repeat the launch warnings at exit, where a long session hasn't
        scrolled them away. Never raises."""
        warnings = self.backend.host_warnings
        if warnings:
            print("\nrepo warnings (for you; the session was not told):\n"
                  + "\n".join(f"  - {w}" for w in warnings),
                  file=sys.stderr)

    def export(self):
        """Copy the work out of the still-running container (after the
        session, before teardown); a no-op for backends that push."""
        if self.container_up:
            self.backend.export(self.box, self.session_id)

    def land(self):
        """Bring the session's work back (PRs, or an import). Runs AFTER
        teardown, so nothing the session left running can change it. Never
        raises."""
        # A kept work volume goes once its work is safe, but only after the
        # background delete has finished (wait_volumes), never beside it.
        self._release_kept = bool(self.backend.land(self.session_id))

    def teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        self.backend.revoke()  # never raises: container cleanup always runs
        if self.container_up:
            self.box.delete()
            log(f"destroyed container {self.name}")
        # Volumes can only go once the instance no longer uses them, and
        # incus removes it asynchronously, so this can retry for a while.
        # Landing needs none of it: run it beside landing, and make
        # wait_volumes() (called last, in a finally) the point where it is
        # known to have finished.
        if self.volumes.created:
            self._volumes_thread = threading.Thread(
                target=self._delete_volumes, name="volume-delete")
            self._volumes_thread.start()

    def _delete_volumes(self):
        try:
            self.volumes.delete()
        except Exception as e:  # a thread's exception would otherwise vanish
            log(f"WARNING: volume cleanup failed ({e}); "
                "the next launch will sweep leaked volumes")

    def wait_volumes(self):
        """Block until the background volume delete has finished. Always call
        this before the launcher exits, so cleanup is never left half done."""
        t, self._volumes_thread = self._volumes_thread, None
        if t is not None:
            t.join()
        if self._release_kept:
            self._release_kept = False
            self.volumes.release()


# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("repository", nargs="?", metavar="REPOSITORY",
                   help="repo to clone with a repo-scoped deploy key: a bare "
                        "name is looked up under --github-user, owner/name "
                        "is used as given")
    p.add_argument("--repo", metavar="OWNER/NAME",
                   help="same as REPOSITORY but requires the full owner/name")
    p.add_argument("--local", metavar="PATH",
                   help="work on a git repository on this machine instead of "
                        "GitHub: its --base branch is copied in as a git "
                        "bundle, and the session's sbx/<session>/* branches "
                        "are imported back into it when the session ends "
                        "(no network credential; see "
                        "docs/specs/08-local-backend.md)")
    p.add_argument("--base", metavar="BRANCH",
                   help="with --local: the branch to start from (default: "
                        "the repository's checked-out branch)")
    p.add_argument("--recover", action="store_true",
                   help="with --local: import the work that crashed launches "
                        "left on kept volumes (each is mounted read-only in a "
                        "throwaway container to export it), then exit")
    p.add_argument("--account-key", action="store_true",
                   help="no repo: register the key on your whole GitHub "
                        "account instead (needs admin:public_key; the "
                        "sandbox can then push to any repo you can). "
                        "Required to run without a repository")
    p.add_argument("--github-user", metavar="USER",
                   default=os.environ.get("CLAUDE_SANDBOX_GITHUB_USER")
                   or DEFAULT_GITHUB_USER,
                   help="GitHub account for bare repo names, and the only "
                        "account `gh` may be logged in as (default: "
                        f"$CLAUDE_SANDBOX_GITHUB_USER or {DEFAULT_GITHUB_USER})")
    p.add_argument("--no-user-check", action="store_true",
                   help="don't require gh to be logged in as --github-user")
    p.add_argument("--no-update-check", action="store_true",
                   help="don't check whether this checkout is behind its "
                        "upstream (and offer to git pull)")
    p.add_argument("--read-only", action="store_true",
                   help="make the deploy key read-only (no push)")
    p.add_argument("--no-pr", action="store_true",
                   help="don't open PRs for the session's branches when it "
                        "ends (they stay on GitHub under sbx/<session>/)")
    p.add_argument("--no-prefetch", action="store_true",
                   help="make the launch's GitHub reads one at a time instead "
                        "of starting the independent ones together")
    p.add_argument("--timings", action="store_true",
                   help="print where the launch spent its time (stages, and "
                        "how many gh / incus calls ran) when it ends")
    p.add_argument("--timings-json", metavar="PATH",
                   help="also write the timings as JSON (implies --timings), "
                        "to compare runs")
    p.add_argument("--no-provenance", action="store_true",
                   help=f"don't post the `{sandbox_provenance.CONTEXT}` commit "
                        "status (every commit signed by the session's own key "
                        "or yours) on the session's PRs")
    p.add_argument("--no-hardening-prompt", action="store_true",
                   help="don't offer to tighten the repo's Actions settings "
                        "(read-only default token, Actions can't approve PRs); "
                        "the check and its warnings still run")
    p.add_argument("--no-protect-prompt", action="store_true",
                   help="don't offer to apply the default-branch protection "
                        "ruleset when the repo lacks one (the warning still "
                        "prints)")
    p.add_argument("--land-all", action="store_true",
                   help="open PRs for every session's unmerged sbx/* branch "
                        "in the repo, then exit (recovery after a crashed "
                        "launcher; also picks up live sessions' work)")
    p.add_argument("--image", default=None, metavar="IMAGE",
                   help="base OS image to launch. Overrides the repo's `image:` "
                        f"in {sandbox_deps.DEPS_FILE}, which overrides the "
                        f"default ({DEFAULT_IMAGE}, Ubuntu 26.04 LTS)")
    p.add_argument("--cli", default=DEFAULT_CLI, choices=["incus", "lxc"],
                   help=f"container CLI (default: {DEFAULT_CLI})")
    p.add_argument("--name", help="session id and container name: letters, "
                   "digits, hyphens (default: Docker-style random, e.g. "
                   "quirky-hopper)")
    p.add_argument("--git-name", metavar="NAME",
                   help="git user.name inside the sandbox "
                        "(default: --github-user)")
    p.add_argument("--git-email", metavar="EMAIL",
                   default=os.environ.get("CLAUDE_SANDBOX_GIT_EMAIL")
                   or DEFAULT_GIT_EMAIL,
                   help="git user.email inside the sandbox (default: "
                        f"$CLAUDE_SANDBOX_GIT_EMAIL or {DEFAULT_GIT_EMAIL})")
    p.add_argument("--shell", action="store_true",
                   help="open a login shell instead of launching claude")
    p.add_argument("--no-final-check", action="store_true",
                   help="skip the repo's `check:` scripts (claude_sandbox_deps"
                        ".yaml) that run when you exit Claude, before "
                        "teardown, and reopen the session if they fail")
    p.add_argument("--tmux", action="store_true",
                   help="wrap the session in tmux inside the container "
                        "(mouse scrolling, 100k-line history)")
    p.add_argument("--nesting", action="store_true",
                   help="allow containers inside the sandbox "
                        "(security.nesting=true) and set up incus if the "
                        "repo's deps installed it")
    p.add_argument("--no-cache", action="store_true",
                   help="launch --image directly and install everything at "
                        "boot instead of using the cached base image")
    p.add_argument("--rebuild", action="store_true",
                   help="rebuild the cached base and deps images even if "
                        "they are fresh")
    p.add_argument("--max-age", type=float, default=7, metavar="DAYS",
                   help="a cached image older than this is stale: it is still "
                        "used (see --hard-max-age) and refreshed in the "
                        "background (default: 7)")
    p.add_argument("--hard-max-age", type=float, default=30, metavar="DAYS",
                   help="a cached image older than this is rebuilt before "
                        "launching; between --max-age and this it is used as "
                        "is while a background process refreshes it "
                        "(default: 30; 0 = always rebuild at --max-age)")
    p.add_argument("--refresh-images", action="store_true",
                   help="internal: rebuild stale cached images and exit (the "
                        "launcher runs this detached after launching on one)")
    p.add_argument("--model", metavar="MODEL",
                   default=os.environ.get("CLAUDE_SANDBOX_MODEL")
                   or DEFAULT_MODEL,
                   help="model for the Claude Code session: an alias "
                        "(opus, sonnet, haiku) or a full model ID "
                        f"(default: $CLAUDE_SANDBOX_MODEL or {DEFAULT_MODEL}); "
                        "'default' leaves the choice to Claude Code")
    p.add_argument("--disk-size", type=size_arg, default=DEFAULT_DISK_SIZE,
                   metavar="SIZE",
                   help="storage quota for the root disk of the sandbox and "
                        f"of image builds (default: {DEFAULT_DISK_SIZE}; 0 = "
                        "no limit; needs a pool with quota support)")
    p.add_argument("--volume-size", type=size_arg, default=DEFAULT_VOLUME_SIZE,
                   metavar="SIZE",
                   help="quota for each separate volume mounted over /tmp and "
                        "the clone directory, so a full one can't take down "
                        f"the OS (default: {DEFAULT_VOLUME_SIZE}; 0 = keep "
                        "everything on the root disk)")
    p.add_argument("--grow-pool", type=size_arg, metavar="SIZE",
                   help="first grow the incus storage pool to SIZE (loop-"
                        "backed pools only; changes the host, shared by all "
                        "sandboxes). Without it the launcher only checks "
                        "free space and tells you the command")
    p.add_argument("--no-deps", action="store_true",
                   help=f"don't install the repo's {sandbox_deps.DEPS_FILE}")
    p.add_argument("--usb", action="append", default=[], metavar="VID:PID",
                   type=usb_spec,
                   help="pass the host USB device(s) with this id into the "
                        "sandbox (repeatable, e.g. 2e8a:000c). The repo's "
                        f"{sandbox_host.HOST_FILE} can only request devices; "
                        "this flag, --usb-allow or a prompt grants them")
    p.add_argument("--no-usb", action="store_true",
                   help="attach no USB devices, ignoring --usb and the repo's "
                        "requests")
    p.add_argument("--usb-allow", dest="usb_allow", default=DEFAULT_USB_ALLOW,
                   metavar="PATH",
                   help="allowlist of repo-requested devices attached without "
                        "a prompt: lines of 'VID:PID [owner/repo]' "
                        f"(default: {DEFAULT_USB_ALLOW})")
    p.add_argument("--claude-md", metavar="PATH",
                   default=os.path.join(os.path.dirname(
                       os.path.abspath(__file__)), "sandbox_CLAUDE.md"),
                   help="template for ~/.claude/CLAUDE.md in the sandbox "
                        "(default: sandbox_CLAUDE.md next to this script)")
    p.add_argument("--setup-token", action="store_true",
                   help="run `claude setup-token` in a throwaway sandbox and "
                        "save the long-lived login token to --token-file, "
                        "then exit (no GitHub key, no clone)")
    p.add_argument("--token-file", default=DEFAULT_TOKEN_FILE, metavar="PATH",
                   help="Claude login token passed into every session "
                        f"(default: {DEFAULT_TOKEN_FILE}); overridden by "
                        "CLAUDE_CODE_OAUTH_TOKEN on the host")
    return p


def normalize_args(args):
    """Resolve the repository and defaults in place; ValueError on bad input.

    A repository is required unless --setup-token (no GitHub involved) or the
    explicit --account-key (an account-wide key is a much bigger grant, so it
    is never the silent result of forgetting the repo).
    """
    if args.repository and args.repo:
        raise ValueError("give the repository once: either REPOSITORY or --repo")
    if args.base and not args.local:
        raise ValueError("--base only applies with --local")
    if args.recover and not args.local:
        raise ValueError("--recover only applies with --local")
    if args.local:
        clash = [f for f, on in (("a repository", args.repository or args.repo),
                                 ("--account-key", args.account_key),
                                 ("--land-all", args.land_all),
                                 ("--read-only", args.read_only)) if on]
        if clash:
            raise ValueError(f"--local can't be combined with {', '.join(clash)}")
        args.local = resolve_local(args.local)
    if args.repo:
        args.repo = validate_repo(args.repo)
    elif args.repository:
        args.repo = resolve_repo(args.repository, args.github_user)
    if args.account_key and args.repo:
        raise ValueError("--account-key and a repository are mutually exclusive")
    if not (args.repo or args.local or args.account_key or args.setup_token
            or args.refresh_images):
        raise ValueError("a repository is required (or --account-key to use "
                         "an account-wide key, or --setup-token)")
    if args.land_all and not args.repo:
        raise ValueError("--land-all needs a repository")
    if args.name:
        sandbox_sessions.validate_session(args.name)
    args.git_name = args.git_name or args.github_user
    return args


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    try:
        normalize_args(args)
    except ValueError as e:
        p.error(str(e))
    if not os.path.isfile(args.claude_md):
        p.error(f"--claude-md: no such file: {args.claude_md}")

    timings = None
    if args.timings or args.timings_json:
        timings = sandbox_timing.Timings()
        sandbox_timing.install(timings)
    try:
        return _launch(args)
    finally:
        sandbox_github.clear_prefetch()
        if timings is not None:
            sandbox_timing.install(None)
            print("\n" + timings.report(), file=sys.stderr)
            if args.timings_json:
                try:
                    sandbox_timing.write_json(timings, args.timings_json)
                    log(f"timings written to {args.timings_json}")
                except OSError as e:
                    log(f"warning: couldn't write {args.timings_json}: {e}")


def _launch(args):
    if args.refresh_images:
        return refresh_images(args)
    if not args.no_update_check:
        with sandbox_timing.stage("update check (git fetch)"):
            sandbox_update.check_for_updates(
                os.path.dirname(os.path.abspath(__file__)), sys.argv,
                interactive=sys.stdin.isatty(), ask=input)

    if args.land_all:
        return sandbox_backend_github.land_all(args)
    if args.recover:
        return sandbox_backend_local.recover(args, lambda: recovery_image(args))

    box = Sandbox(args)
    sandbox_github.start_prefetch(box.backend.prefetch_plan())
    if not args.setup_token:
        with sandbox_timing.stage("host checks (login, scopes)"):
            bad = box.backend.check_host()  # fail fast, before building anything
            if bad:
                print(f"error: {bad}", file=sys.stderr)
                return 1

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    with sandbox_timing.stage("incus pool check"):
        pool_err = sandbox_pool.check(args.cli, args.grow_pool)
    if pool_err:
        print(f"error: {pool_err}", file=sys.stderr)
        return 1

    try:
        if not args.setup_token:
            with sandbox_timing.stage("usb requests + deps file"):
                box.resolve_usb()  # prompt now, not after a long image build
                box.load_deps()  # before the image: it may pick the base OS
            with sandbox_timing.stage("preflight (protection, stale keys)"):
                box.backend.preflight(lambda: Container.list_names(args.cli),
                                      box.name)
        source = box.choose_image()
        image = source
        stale: list = []
        swr = dict(hard_max_age_days=args.hard_max_age or None,
                   on_stale=lambda kind, alias: stale.append((kind, alias)))
        with sandbox_timing.stage("images (cache lookup / build)"):
            if not args.no_cache:
                image = ensure_base_image(args.cli, source,
                                          args.max_age, args.rebuild,
                                          args.disk_size, **swr)
            if box.deps and not args.no_cache:
                baked = ensure_deps_image(args.cli, image, box.deps,
                                          args.max_age, args.rebuild,
                                          args.disk_size, **swr)
                if baked:
                    image, box.deps_baked = baked, True
                    box.deps_note += ("\n(Preinstalled in a cached image; edits "
                                      "to the file apply on the next launch.)")
        with sandbox_timing.stage("container launch + boot"):
            box.launch(image)
        if stale and not args.setup_token:   # after the launch: never competes with it
            spawn_refresh(args)
        if args.setup_token:
            box.install_claude()
            return box.setup_token()
        with sandbox_timing.stage("volumes"):
            box.attach_volumes()  # before the clone, so it lands on its own volume
        with sandbox_timing.stage("install deps"):
            box.install_deps()  # before any credential is in the container
            box.setup_nesting()
            box.attach_usb()
        with sandbox_timing.stage("ssh key + deploy key + git config"):
            box.provision_key()
        with sandbox_timing.stage("claude install check"):
            box.install_claude()
        with sandbox_timing.stage("clone"):
            workdir = box.clone()
        with sandbox_timing.stage("repo setup scripts"):
            box.run_setup(workdir)  # before Claude credentials reach the container
        with sandbox_timing.stage("session tools + CLAUDE.md"):
            box.install_session_tools()
            box.write_claude_md(workdir)
        with sandbox_timing.stage("session (interactive)"):
            box.session(workdir)
    except KeyboardInterrupt:
        log("interrupted")
    except (subprocess.CalledProcessError, RuntimeError) as e:
        log(f"error: {e}")
        err = getattr(e, "stderr", None)
        if err:
            print(err.decode(errors="replace") if isinstance(err, bytes)
                  else err, file=sys.stderr)
        return 1
    finally:
        # Teardown FIRST: revoke the key and destroy the container, so nothing
        # the session left running (a nohup'd loop, a tmux pane) can push
        # between the landing's checks and the PRs it opens. Landing needs
        # only the user's gh login, never the key or the container.
        try:
            with sandbox_timing.stage("export work"):
                box.export()  # container still up; never raises
            with sandbox_timing.stage("teardown"):
                box.teardown()
        finally:
            try:
                with sandbox_timing.stage("land: PRs, provenance"):
                    box.land()
                box.report_host_warnings()
            finally:
                # Volume deletion runs beside landing; finish it before exit.
                with sandbox_timing.stage("volumes: wait for delete"):
                    box.wait_volumes()
    return 0


if __name__ == "__main__":
    sys.exit(main())
