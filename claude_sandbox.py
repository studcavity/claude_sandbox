#!/usr/bin/env python3
"""
claude_sandbox.py - throwaway LXD/Incus container for a Claude Code session.

What it does:
  1. Launches an ephemeral container.
  2. Generates an SSH key *inside* the container. The private key never
     touches the host.
  3. Registers the public key with GitHub, either as a deploy key on one
     repo (--repo, recommended) or as an account key (default).
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

GitHub auth: set GITHUB_TOKEN / GH_TOKEN, or be logged in with `gh`.
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

Examples:
  ./claude_sandbox.py --repo myuser/myproject      # deploy key + clone
  ./claude_sandbox.py                              # account-wide key
  ./claude_sandbox.py --repo me/proj --model opus  # pick the session model
  ./claude_sandbox.py --cli incus --image images:ubuntu/24.04 --repo me/proj
"""
import argparse
import functools
import getpass
import os
import shlex
import string
import subprocess
import signal
import sys
import uuid

import sandbox_deps
from sandbox_containers import HOME, UID, USER, Container
from sandbox_github import (GitHubError, gh_api, github_token, token_scopes,
                            validate_repo, workflow_push_warning)
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

DEFAULT_TOKEN_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "claude_sandbox", "oauth_token")
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


def parse_probe(out):
    """Parse the key=value lines printed by the container probe."""
    facts = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return {"sudo": facts.get("sudo", "").strip() == "yes",
            "tools": facts.get("tools", "").split(),
            "branch": facts.get("branch", "").strip()}


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
                     deps_note):
    """Fill the sandbox CLAUDE.md template.

    The template is plain Markdown with string.Template placeholders
    ($container, $access, $user, $uid, $home, $sudo, $tools, $branch, $deps,
    $deps_file); write $$ for a literal dollar sign. `facts` comes from
    parse_probe().
    """
    branch = facts["branch"]
    return string.Template(template).substitute(
        container=container,
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
    )

def check_workflow_scope(token):
    """Warn (never fail) if the sandbox won't be able to push workflow files."""
    try:
        msg = workflow_push_warning(token_scopes(token))
    except (GitHubError, OSError) as e:
        msg = workflow_push_warning(None) + f" ({e})"
    if msg:
        log(f"warning: {msg}")


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
class Sandbox:
    def __init__(self, args):
        self.args = args
        self.cli = args.cli
        self.name = args.name or f"claude-{uuid.uuid4().hex[:8]}"
        self.box = Container(self.cli, self.name)
        self.key_id = None
        self.container_up = False
        self._torn_down = False
        self.deps = None
        self.deps_baked = False
        self.deps_note = ("No repo was cloned, so there is no "
                          f"`{sandbox_deps.DEPS_FILE}`.")

    @functools.cached_property
    def token(self):
        # Looked up on first use so --setup-token works without GitHub auth.
        return github_token()

    # -- lifecycle ------------------------------------------------------------
    def launch(self, image):
        log(f"launching ephemeral container {self.name} ({image})")
        self.box.launch(image, ephemeral=True)
        self.container_up = True
        self.box.wait_network()
        self.box.self_check()  # fail fast, before any real work

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
                         self.token, raw=True)
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

    def install_deps(self):
        """Install the deps into this container unless the image has them.

        Runs before the SSH key and Claude credentials exist in the container,
        so installers never see them.
        """
        if self.deps and not self.deps_baked:
            apply_deps(self.box, self.deps)

    def provision_key(self):
        log("generating SSH key inside the container")
        self.box.user(f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                      f"ssh-keygen -q -t ed25519 -N '' "
                      f"-C {shlex.quote(self.name)} -f ~/.ssh/id_ed25519")
        pubkey = self.box.user("cat ~/.ssh/id_ed25519.pub",
                               capture=True).stdout.strip()

        # Pin GitHub host keys from the API (over the host's TLS) instead of
        # trusting ssh-keyscan on first use.
        meta = gh_api("GET", "/meta", self.token)
        known = "".join(f"github.com {k}\n" for k in meta["ssh_keys"])
        self.box.user("cat >> ~/.ssh/known_hosts", input=known)

        title = f"ephemeral {self.name}"
        if self.args.repo:
            log(f"adding deploy key to {self.args.repo}")
            resp = gh_api("POST", f"/repos/{self.args.repo}/keys", self.token,
                          {"title": title, "key": pubkey,
                           "read_only": self.args.read_only})
        else:
            log("adding SSH key to your GitHub account")
            resp = gh_api("POST", "/user/keys", self.token,
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
        repo_dir = f"{HOME}/{self.args.repo.split('/')[1]}"
        log(f"cloning {self.args.repo}")
        self.box.user(f"git clone git@github.com:{self.args.repo}.git "
                      f"{shlex.quote(repo_dir)}")
        return repo_dir

    def write_claude_md(self, workdir):
        """Render the --claude-md template into ~/.claude/ in the container."""
        probe = self.box.user(
            "sudo -n true 2>/dev/null && echo sudo=yes || echo sudo=no; "
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
                              deps_note=self.deps_note)
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

    def teardown(self):
        if self._torn_down:
            return
        self._torn_down = True

        if self.key_id is not None:
            path = (f"/repos/{self.args.repo}/keys/{self.key_id}"
                    if self.args.repo else f"/user/keys/{self.key_id}")
            try:
                gh_api("DELETE", path, self.token)
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


# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", help="owner/name: use a repo-scoped deploy key "
                                  "and clone it (recommended)")
    p.add_argument("--read-only", action="store_true",
                   help="make the deploy key read-only (no push)")
    p.add_argument("--image", default="ubuntu:24.04",
                   help="image to launch (default: ubuntu:24.04; "
                        "Incus users want images:ubuntu/24.04)")
    p.add_argument("--cli", default="lxc", choices=["lxc", "incus"])
    p.add_argument("--name", help="container name (default: random)")
    p.add_argument("--git-name", help="git user.name inside the sandbox")
    p.add_argument("--git-email", help="git user.email inside the sandbox")
    p.add_argument("--shell", action="store_true",
                   help="open a login shell instead of launching claude")
    p.add_argument("--tmux", action="store_true",
                   help="wrap the session in tmux inside the container "
                        "(mouse scrolling, 100k-line history)")
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
    p.add_argument("--no-deps", action="store_true",
                   help=f"don't install the repo's {sandbox_deps.DEPS_FILE}")
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


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if args.repo:
        try:
            validate_repo(args.repo)
        except ValueError as e:
            p.error(f"--repo: {e}")
    if not os.path.isfile(args.claude_md):
        p.error(f"--claude-md: no such file: {args.claude_md}")

    box = Sandbox(args)
    if not args.setup_token:
        box.token  # fail fast, before building or launching anything
        if not args.read_only:
            check_workflow_scope(box.token)

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    try:
        image = args.image
        if not args.no_cache:
            image = ensure_base_image(args.cli, args.image,
                                      args.max_age, args.rebuild)
        if not args.setup_token and box.load_deps() and not args.no_cache:
            baked = ensure_deps_image(args.cli, image, box.deps,
                                      args.max_age, args.rebuild)
            if baked:
                image, box.deps_baked = baked, True
                box.deps_note += ("\n(Preinstalled in a cached image; edits "
                                  "to the file apply on the next launch.)")
        box.launch(image)
        if args.setup_token:
            box.install_claude()
            return box.setup_token()
        box.install_deps()  # before any credential is in the container
        box.provision_key()
        box.install_claude()
        workdir = box.clone()
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
        box.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
