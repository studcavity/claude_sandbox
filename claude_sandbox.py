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
  - Keys inherit the token's scopes when pushing: without `workflow`
    (gh auth refresh -s workflow) the sandbox can't push .github/workflows/
    changes. The launcher warns at startup when that's the case.
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
  ./claude_sandbox.py --account-key                # no repo: account-wide key
  ./claude_sandbox.py --setup-token                # one-time Claude login token
"""
import argparse
import functools
import getpass
import os
import re
import shlex
import string
import subprocess
import signal
import sys

if __name__ == "__main__":
    # Standard library only, before the third-party imports below: ensures a
    # venv with requirements.txt and re-execs into it. Skipped on import (tests).
    import sandbox_bootstrap
    sandbox_bootstrap.ensure_venv(os.path.dirname(os.path.abspath(__file__)))

import sandbox_deps
import sandbox_host
import sandbox_pool
import sandbox_volumes
import sandbox_actions
import sandbox_check
import sandbox_land
import sandbox_protect
import sandbox_sessions
import sandbox_update
from sandbox_containers import HOME, UID, USER, Container
from sandbox_github import (GitHubError, gh_api, github_login, resolve_repo,
                            token_scopes, validate_repo, workflow_push_warning)
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
                   tmux=False):
    """The bash -lc script that starts the interactive session."""
    prog = "bash -l" if shell else "claude"
    if not shell and model.lower() != "default":
        prog += f" --model {shlex.quote(model)}"
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


def access_lines(workdir, repo, branch, read_only):
    if not repo:
        return [f"- Working directory: `{workdir}`. No repo was cloned.",
                "- An SSH key is registered on the user's GitHub "
                "**account** for this session, so you can clone and "
                "push to any repo they can access."]
    return [
        f"- Working copy: `{workdir}`, a clone of `github.com/{repo}`"
        + (f" (branch `{branch}`)" if branch else "") + ".",
        f"- GitHub access is an SSH **deploy key scoped to `{repo}` "
        "only**. Other repos can't be cloned or pushed over SSH. "
        "`https://github.com/` URLs are rewritten to SSH.",
        "- The deploy key is **read-only**: you cannot push. Tell the "
        "user early that commits will be lost when the sandbox ends "
        "unless they export them some other way."
        if read_only else
        "- The deploy key has write access, so you can push.",
    ]


def render_claude_md(template, facts, container, workdir, repo, read_only,
                     deps_note, usb_devices=(), cli="incus",
                     disk_note="", session=None, protection_note=""):
    """Fill the sandbox CLAUDE.md template.

    The template is plain Markdown with string.Template placeholders
    ($container, $access, $user, $uid, $home, $sudo, $tools, $branch, $deps,
    $deps_file, $host_file, $usb, $disk, $session, $prefix, $repo, $protection); write $$ for a
    literal dollar sign. `facts` comes from
    parse_probe().
    """
    branch = facts["branch"]
    return string.Template(template).substitute(
        container=container,
        session=session or container,
        repo=repo or "(no repo)",
        protection=protection_note or "Not applicable (no repo, or a "
                                      "read-only key).",
        prefix=sandbox_sessions.branch_prefix(session or container),
        access="\n".join(access_lines(workdir, repo, branch, read_only)),
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

def protection_note(repo, branch, state, checks=()):
    """Repo-specific text for the sandbox CLAUDE.md about default-branch
    protection and required status checks."""
    facts = {
        "protected": f"`{branch}` already requires pull requests.",
        "applied": f"The launcher applied the protection ruleset to `{branch}` "
                   "at the start of this session.",
        "unprotected": f"`{branch}` does **not** require pull requests: no "
                       "ruleset is enforcing the branch workflow, so it is "
                       "only a convention here.",
        "unknown": f"Whether `{branch}` is protected could not be verified.",
    }[state]
    if checks:
        req = ("Required status checks: " + ", ".join(f"`{c}`" for c in checks)
               + ".")
    elif state == "applied":
        req = "No status checks are required yet."
    else:
        req = "Required status checks: none known."
    return f"{facts} {req} (Repo: `{repo}`.)"


def ask_protect(repo, branch, effects):
    """Interactive yes/no (default no) to apply the protection ruleset."""
    print(f"\n\033[1m{repo}: `{branch}` does not require pull requests.\033[0m"
          "\nApply the protection ruleset? It would:", file=sys.stderr)
    for e in effects:
        print(f"  - {e}", file=sys.stderr)
    try:
        return input("Apply branch protections to this repo? [y/N] "
                     ).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask_ruleset(repo, name, effects):
    """Interactive yes/no (default no) to apply a ruleset the repo defines."""
    print(f"\n\033[1m{repo} defines ruleset `{name}` "
          "(rulesets/ on its default branch), not applied yet.\033[0m"
          "\nApplying it would:", file=sys.stderr)
    for e in effects:
        print(f"  - {e}", file=sys.stderr)
    try:
        return input(f"Apply ruleset `{name}` to this repo? [y/N] "
                     ).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask_checks(branch, checks):
    """Interactive yes/no (default no) to also require these CI checks."""
    print("\nThese checks passed on the latest commit of "
          f"`{branch}`:", file=sys.stderr)
    for c in checks:
        print(f"  - {c}", file=sys.stderr)
    print("Requiring them also makes PRs wait for them, and (strict mode) "
          "need to be up to date with the base. A name that never reports "
          "would block every merge.", file=sys.stderr)
    try:
        return input("Require these checks too? [y/N] ").strip().lower() in (
            "y", "yes")
    except EOFError:
        return False


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


def check_expected_user(login, expected):
    """Error text when gh is logged in as someone unexpected, else None."""
    if login.lower() == expected.lower():
        return None
    return (f"gh is authenticated as {login!r}, expected {expected!r} "
            "(--github-user); refusing to continue. Switch accounts with "
            "`gh auth switch`, or pass --github-user / --no-user-check if "
            "that is intended.")


def setup_paths(deps):
    """Repo-relative `setup:` script paths from parsed deps (they run with
    extra privilege in future sandboxes, so a PR touching them is held)."""
    return [s["path"] for s in (deps or {}).get("setup") or []]


def report_landing(results, session=None):
    """Print the exit summary of PRs the launcher opened (to stderr)."""
    print("\n" + sandbox_land.format_landing(
        results, session, color=sys.stderr.isatty()), file=sys.stderr)


def action_limits(args):
    """The caps for Sandbox-Action filing, from --max-actions / --max-issues."""
    return {"actions": args.max_actions, "issues": args.max_issues}


def report_statuses(outcomes):
    """Print the exit summary of `sandbox/check` statuses (to stderr)."""
    text = sandbox_check.format_status_outcomes(outcomes)
    if text:
        print("\n" + text, file=sys.stderr)


def report_actions(outcomes):
    """Print the exit summary of follow-up actions (to stderr)."""
    text = sandbox_actions.format_outcomes(outcomes)
    if text:
        print("\n" + text, file=sys.stderr)


def check_workflow_scope():
    """Warn (never fail) if the sandbox won't be able to push workflow files."""
    try:
        msg = workflow_push_warning(token_scopes())
    except (GitHubError, OSError) as e:
        msg = workflow_push_warning(None) + f" ({e})"
    if msg:
        log(f"warning: {msg}")


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


def repo_workdir(repo):
    return f"{HOME}/{repo.split('/')[1]}" if repo else HOME


class Sandbox:
    def __init__(self, args):
        self.args = args
        self.cli = args.cli
        # The container name doubles as the session id (key title, branches).
        self.name = args.name or sandbox_sessions.pick_name(
            Container.list_names(self.cli))
        self.session_id = sandbox_sessions.validate_session(self.name)
        self.protection_note = ""
        self.box = Container(self.cli, self.name)
        self.key_id = None
        self.container_up = False
        self._torn_down = False
        self.deps = None
        self.deps_baked = False
        self.image = None
        self.usb_devices = []
        self.volumes = sandbox_volumes.Volumes(self.cli, self.box, [], None)
        self.deps_note = ("No repo was cloned, so there is no "
                          f"`{sandbox_deps.DEPS_FILE}`.")

    @functools.cached_property
    def login(self):
        # Looked up on first use so --setup-token works without GitHub auth.
        # Also proves `gh` is installed and logged in, before anything is built.
        return github_login()

    # -- lifecycle ------------------------------------------------------------
    def launch(self, image):
        log(f"launching ephemeral container {self.name} ({image})")
        self.box.launch(image, ephemeral=True, config=launch_config(self.args),
                        root_size=self.args.disk_size)
        self.container_up = True
        self.box.wait_network()
        self.box.self_check()  # fail fast, before any real work
        # Lets repo scripts refuse to run anywhere but inside a sandbox.
        self.box.put_file(SANDBOX_MARKER, self.name.encode() + b"\n", "0644")

        # No-ops when launched from the cached image.
        self.box.root("command -v git >/dev/null && command -v curl >/dev/null || "
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
        mounts = sandbox_volumes.plan(self.name, repo_workdir(self.args.repo),
                                      self.args.volume_size)
        self.volumes = sandbox_volumes.Volumes(
            self.cli, self.box, mounts, self.args.volume_size)
        self.volumes.attach()

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
        if not self.args.repo:
            return None
        if self.args.no_deps:
            self.deps_note = f"Skipped (`--no-deps`): `{f}` was not applied."
            return None
        try:
            raw = gh_api("GET", f"/repos/{self.args.repo}/contents/{f}",
                         raw=True)
        except GitHubError as e:
            if e.code != 404:
                raise
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
        if a.repo:
            try:
                raw = gh_api("GET", f"/repos/{a.repo}/contents/"
                             f"{sandbox_host.HOST_FILE}", raw=True)
                requests = sandbox_host.parse(raw)["usb"]
            except GitHubError as e:
                if e.code != 404:
                    raise
        try:
            allow = sandbox_host.load_allowlist(a.usb_allow)
        except ValueError as e:
            raise RuntimeError(f"USB allowlist: {e}") from None
        self.usb_devices = sandbox_host.resolve_usb(
            requests, a.usb, allow, a.repo,
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
        log("generating SSH key inside the container")
        self.box.user(f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                      f"ssh-keygen -q -t ed25519 -N '' "
                      f"-C {shlex.quote(self.name)} -f ~/.ssh/id_ed25519")
        pubkey = self.box.user("cat ~/.ssh/id_ed25519.pub",
                               capture=True).stdout.strip()

        # Pin GitHub host keys from the API (over the host's TLS) instead of
        # trusting ssh-keyscan on first use.
        meta = gh_api("GET", "/meta")
        known = "".join(f"github.com {k}\n" for k in meta["ssh_keys"])
        self.box.user("cat >> ~/.ssh/known_hosts", input=known)

        title = sandbox_sessions.key_title(sandbox_sessions.host_id(),
                                           self.session_id)
        if self.args.repo:
            log(f"adding deploy key to {self.args.repo}")
            resp = gh_api("POST", f"/repos/{self.args.repo}/keys",
                          {"title": title, "key": pubkey,
                           "read_only": self.args.read_only})
        else:
            log("adding SSH key to your GitHub account")
            resp = gh_api("POST", "/user/keys",
                          {"title": title, "key": pubkey})
        self.key_id = resp["id"]

        self.box.user('git config --global url."git@github.com:".insteadOf '
                      '"https://github.com/"')
        if self.args.git_name:
            self.box.user("git config --global user.name "
                          + shlex.quote(self.args.git_name))
        if self.args.git_email:
            self.box.user("git config --global user.email "
                          + shlex.quote(self.args.git_email))
        # Linear history: rebase on pull, refuse non-fast-forward merges.
        self.box.user("git config --global pull.rebase true && "
                      "git config --global rebase.autoStash true && "
                      "git config --global merge.ff only && "
                      "git config --global push.autoSetupRemote true")

    def install_claude(self):
        if self.box.user("command -v claude >/dev/null", check=False).returncode == 0:
            return  # baked into the cached image
        log("installing Claude Code")
        self.box.user(CLAUDE_INSTALL, capture=True)

    def clone(self):
        if not self.args.repo:
            return HOME
        repo_dir = repo_workdir(self.args.repo)
        log(f"cloning {self.args.repo}")
        self.box.user(f"git clone git@github.com:{self.args.repo}.git "
                      f"{shlex.quote(repo_dir)}")
        if not self.args.read_only:
            # Never work on the default branch: concurrent sessions each get
            # their own branch under sbx/<session>/ and land through PRs.
            self.box.user(f"git -C {shlex.quote(repo_dir)} checkout -q -b "
                          + shlex.quote(sandbox_sessions.work_branch(
                              self.session_id)))
        return repo_dir

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
                              repo=self.args.repo,
                              read_only=self.args.read_only,
                              deps_note=self.deps_note,
                              session=self.session_id,
                              protection_note=self.protection_note,
                              usb_devices=self.usb_devices, cli=self.cli,
                              disk_note=self.disk_note())
        self.box.user("mkdir -p ~/.claude && cat > ~/.claude/CLAUDE.md",
                      input=md)

    def push_secrets(self):
        """Write credentials to a 0600 file in the container via stdin.

        `incus exec --env K=V` would put them on the host's process list.
        Returns a shell prefix that loads and removes the file.
        """
        secrets = claude_secrets(os.environ, self.args.token_file)
        if not secrets:
            log("no Claude credential found; run /login in the session "
                "(or create one with --setup-token)")
            return ""
        log("passing " + " and ".join(secrets) + " into the sandbox")
        self.box.user(f"umask 077 && cat > {SECRETS_FILE}",
                      input=secrets_script(secrets))
        # Already logged in, so skip Claude Code's first-run onboarding.
        self.box.user("[ -e ~/.claude.json ] || "
                      "echo '{\"hasCompletedOnboarding\": true}' > ~/.claude.json")
        return f". {SECRETS_FILE} && rm -f {SECRETS_FILE} && "

    def session(self, workdir):
        env = {k: os.environ[k] for k in TERM_ENV_VARS if os.environ.get(k)}
        script = session_script(workdir, self.push_secrets(),
                                shell=self.args.shell, model=self.args.model,
                                tmux=self.args.tmux)
        cmd = self.box.user_cmd("bash", "-lc", script, env=env)
        cmd[cmd.index("--cwd") + 1] = workdir

        log("starting session (exit Claude Code to tear everything down)")
        with sigint_to_child():
            subprocess.run(cmd)

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

    def preflight_github(self):
        """Advice and housekeeping before launch; never blocks the launch."""
        a = self.args
        if not a.repo or a.read_only:
            return
        self.protect_default_branch()
        try:
            live = Container.list_names(self.cli)
            gone = sandbox_sessions.reap_stale_keys(
                a.repo, sandbox_sessions.host_id(), live | {self.name})
            for key_id in gone:
                log(f"removed stale deploy key {key_id} (its container is gone)")
        except (GitHubError, subprocess.CalledProcessError, OSError) as e:
            log(f"warning: couldn't reap stale deploy keys: {e}")

    def protect_default_branch(self):
        """Check that the default branch requires PRs; if it doesn't, offer
        (on a terminal, default no) to apply rulesets/protect-main.json.
        Advice only: never blocks the launch. Records protection_note for the
        session's CLAUDE.md."""
        a = self.args
        branch, state, checks = "the default branch", "unknown", []
        try:
            branch, warn, state = sandbox_sessions.check_protection(a.repo)
            if warn:
                log(f"warning: {warn}")
            if (state in ("unprotected", "protected")
                    and not a.no_protect_prompt and sys.stdin.isatty()):
                state, checks = self.offer_protection(branch, state)
        except (GitHubError, OSError, ValueError) as e:
            log(f"warning: couldn't check or apply branch protection: {e}")
        self.protection_note = protection_note(a.repo, branch, state, checks)

    def offer_protection(self, branch, state="unprotected"):
        """Ask, then apply. Returns (state, required_checks).

        Rulesets the repo defines itself (rulesets/*.json on its default
        branch) win over the launcher's generic one and are offered even when
        the branch is already protected (they may add e.g. a required check).
        """
        a = self.args
        repo_rs, problems = sandbox_protect.fetch_repo_rulesets(a.repo, branch)
        for p in problems:
            log(f"warning: ignoring {p}")
        existing = sandbox_protect.existing_ruleset_names(a.repo)
        if repo_rs:
            return self.offer_repo_rulesets(
                branch, state, sandbox_protect.pending(repo_rs, existing))
        if state != "unprotected":
            return state, []
        rs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "rulesets", "protect-main.json")
        ruleset = sandbox_protect.load_ruleset(rs_path)
        if ruleset["name"] in existing:
            log(f"ruleset {ruleset['name']!r} already exists but doesn't "
                "require PRs on the default branch; fix it in Settings -> Rules")
            return "unprotected", []
        if not ask_protect(a.repo, branch, sandbox_protect.describe(ruleset,
                                                                    branch)):
            return "unprotected", []
        checks = sandbox_protect.discover_checks(a.repo, branch)
        if checks and not ask_checks(branch, checks):
            checks = []
        sandbox_protect.apply_ruleset(
            a.repo, sandbox_protect.with_checks(ruleset, checks))
        log(f"applied ruleset {ruleset['name']!r} to {a.repo}"
            + (f", requiring: {', '.join(checks)}" if checks else
               " (no required status checks)"))
        return "applied", checks

    def offer_repo_rulesets(self, branch, state, todo):
        """Offer each not-yet-applied repo-defined ruleset, one yes/no each.
        Returns (state, required_checks of the ones applied)."""
        a = self.args
        checks = set()
        for rs in todo:
            if not ask_ruleset(a.repo, rs["name"],
                               sandbox_protect.describe_rules(rs, branch)):
                continue
            sandbox_protect.apply_ruleset(a.repo, rs)
            checks.update(sandbox_protect.required_checks(rs))
            log(f"applied ruleset {rs['name']!r} to {a.repo}")
            if state == "unprotected" and any(
                    r["type"] == "pull_request" for r in rs["rules"]):
                state = "applied"
        return state, sorted(checks)

    def land(self):
        """Open PRs for this session's pushed branches (host-side, with the
        user's gh login). Never raises: teardown must still run."""
        a = self.args
        if not a.repo or a.read_only or self.key_id is None:
            return
        landed = []
        if not a.no_pr:
            try:
                landed = sandbox_land.land(
                    a.repo, self.session_id, setup_paths(self.deps))
                report_landing(landed, self.session_id)
            except Exception as e:
                log(f"warning: couldn't open PRs for {self.session_id}: {e}; "
                    "push state is on GitHub under "
                    f"{sandbox_sessions.branch_prefix(self.session_id)}")
        if landed and not a.no_check_status:
            try:
                report_statuses(sandbox_check.post_session_statuses(
                    a.repo, landed))
            except Exception as e:
                log(f"warning: couldn't post {sandbox_check.STATUS_CONTEXT} "
                    f"for {self.session_id}: {e}")
        if not a.no_actions:
            try:
                report_actions(sandbox_actions.run(
                    a.repo, self.session_id, limits=action_limits(a)))
            except Exception as e:
                log(f"warning: couldn't run follow-up actions for "
                    f"{self.session_id}: {e}")

    def teardown(self):
        if self._torn_down:
            return
        self._torn_down = True

        if self.key_id is not None:
            path = (f"/repos/{self.args.repo}/keys/{self.key_id}"
                    if self.args.repo else f"/user/keys/{self.key_id}")
            try:
                gh_api("DELETE", path)
                log(f"removed GitHub key {self.key_id}")
            except Exception as e:  # never skip container cleanup
                log(f"WARNING: could not delete GitHub key {self.key_id}: {e}")
                log("remove it manually at https://github.com/settings/keys"
                    if not self.args.repo else
                    f"remove it at https://github.com/{self.args.repo}"
                    "/settings/keys")

        if self.container_up:
            self.box.delete()
            log(f"destroyed container {self.name}")
        self.volumes.delete()  # only once the instance no longer uses them


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
    p.add_argument("--no-check-status", action="store_true",
                   help=f"don't post the `{sandbox_check.STATUS_CONTEXT}` commit "
                        "status on the session's PRs (it is only posted where "
                        "the repo's ruleset requires it)")
    p.add_argument("--max-actions", type=int, metavar="N",
                   default=sandbox_actions.MAX_ACTIONS,
                   help="most Sandbox-Action items to carry out per run "
                        "(default %(default)s)")
    p.add_argument("--max-issues", type=int, metavar="N",
                   default=sandbox_actions.MAX_ISSUES_PER_SESSION,
                   help="most issues one session may have in a repo, counting "
                        "ones already filed (default %(default)s)")
    p.add_argument("--no-actions", action="store_true",
                   help="don't act on `Sandbox-Action:` trailers in the "
                        "session's commit messages (e.g. file follow-up issues)")
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
                   help="rebuild the cached image when older than this "
                        "(default: 7)")
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
    if args.repo:
        args.repo = validate_repo(args.repo)
    elif args.repository:
        args.repo = resolve_repo(args.repository, args.github_user)
    if args.account_key and args.repo:
        raise ValueError("--account-key and a repository are mutually exclusive")
    if not (args.repo or args.account_key or args.setup_token):
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

    if not args.no_update_check:
        sandbox_update.check_for_updates(
            os.path.dirname(os.path.abspath(__file__)), sys.argv,
            interactive=sys.stdin.isatty(), ask=input)

    if args.land_all:
        try:
            if not args.no_user_check:
                bad = check_expected_user(github_login(), args.github_user)
                if bad:
                    print(f"error: {bad}", file=sys.stderr)
                    return 1
            landed = sandbox_land.land(args.repo)
            report_landing(landed)
            if landed and not args.no_check_status:
                report_statuses(sandbox_check.post_session_statuses(
                    args.repo, landed))
            if not args.no_actions:
                report_actions(sandbox_actions.run(
                    args.repo, limits=action_limits(args)))
        except GitHubError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    box = Sandbox(args)
    if not args.setup_token:
        try:
            login = box.login  # fail fast, before building or launching anything
        except GitHubError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if not args.no_user_check:
            bad = check_expected_user(login, args.github_user)
            if bad:
                print(f"error: {bad}", file=sys.stderr)
                return 1
        if not args.read_only:
            check_workflow_scope()

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    pool_err = sandbox_pool.check(args.cli, args.grow_pool)
    if pool_err:
        print(f"error: {pool_err}", file=sys.stderr)
        return 1

    try:
        if not args.setup_token:
            box.resolve_usb()  # prompt now, not after a long image build
            box.load_deps()  # before the image: it may pick the base OS
        if not args.setup_token:
            box.preflight_github()
        source = box.choose_image()
        image = source
        if not args.no_cache:
            image = ensure_base_image(args.cli, source,
                                      args.max_age, args.rebuild,
                                      args.disk_size)
        if box.deps and not args.no_cache:
            baked = ensure_deps_image(args.cli, image, box.deps,
                                      args.max_age, args.rebuild,
                                      args.disk_size)
            if baked:
                image, box.deps_baked = baked, True
                box.deps_note += ("\n(Preinstalled in a cached image; edits "
                                  "to the file apply on the next launch.)")
        box.launch(image)
        if args.setup_token:
            box.install_claude()
            return box.setup_token()
        box.attach_volumes()  # before the clone, so it lands on its own volume
        box.install_deps()  # before any credential is in the container
        box.setup_nesting()
        box.attach_usb()
        box.provision_key()
        box.install_claude()
        workdir = box.clone()
        box.run_setup(workdir)  # before Claude credentials reach the container
        box.write_claude_md(workdir)
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
        box.land()  # host-side, uses the user's gh; before the key is revoked
        box.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
