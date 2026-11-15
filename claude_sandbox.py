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
  5. On exit (normal, error, Ctrl-C, SIGTERM, SIGHUP) deletes the GitHub key
     and destroys the container.

GitHub auth: set GITHUB_TOKEN / GH_TOKEN, or be logged in with `gh`.
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
  ./claude_sandbox.py --cli incus --image images:ubuntu/24.04 --repo me/proj
"""
import argparse
import datetime
import functools
import getpass
import hashlib
import json
import os
import shlex
import signal
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import sandbox_deps

API = "https://api.github.com"
USER, UID, HOME = "ubuntu", "1000", "/home/ubuntu"

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

# Cached base image. The alias embeds a hash of the source image and the build
# steps below, so editing either one triggers a rebuild automatically.
IMAGE_PREFIX = "claude-sandbox-"
BASE_PACKAGES = (
    # session essentials
    "git", "curl", "openssh-client", "tmux", "ca-certificates",
    # python
    "python3-pip", "python3-venv", "python3-dev",
    # building native deps
    "build-essential", "pkg-config",
    # search / inspection tools Claude Code reaches for
    "ripgrep", "fd-find", "jq", "tree", "file", "less", "wget",
    "unzip", "zip", "xz-utils",
    # process / system debugging
    "procps", "psmisc", "lsof", "strace", "gdb", "htop",
    "iproute2", "dnsutils", "netcat-openbsd",
)
BASE_ROOT_SETUP = ("export DEBIAN_FRONTEND=noninteractive && "
                   "apt-get update -qq && "
                   "apt-get install -y -qq --no-install-recommends "
                   + " ".join(BASE_PACKAGES) + " && "
                   # Debian/Ubuntu ship fd as fdfind
                   "ln -sf /usr/bin/fdfind /usr/local/bin/fd && "
                   "apt-get clean && rm -rf /var/lib/apt/lists/*")
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
# Reset per-instance identity so every container launched from the image
# gets its own.
BASE_CLEANUP = ("(command -v cloud-init >/dev/null && cloud-init clean --logs);"
                " truncate -s 0 /etc/machine-id; true")

TMUX_CONF = """\
set -g mouse on
set -g history-limit 100000
set -g default-terminal tmux-256color
set -as terminal-features ',*:RGB'
set -g status off
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg):
    print(f"\033[1;36m[sandbox]\033[0m {msg}", file=sys.stderr)


def run(cmd, check=True, capture=False, input=None):
    return subprocess.run(cmd, check=check, text=True,
                          capture_output=capture, input=input)


def github_token():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return run(["gh", "auth", "token"], capture=True).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("No GitHub token: set GITHUB_TOKEN or run `gh auth login`.")


class GitHubError(RuntimeError):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def gh_api(method, path, token, body=None, raw=False):
    """Call the GitHub API; raw=True returns the response body as bytes."""
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": ("application/vnd.github.raw+json" if raw
                       else "application/vnd.github+json"),
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            if raw:
                return data
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        raise GitHubError(f"GitHub {method} {path} -> {e.code}: "
                          f"{e.read().decode(errors='replace')}", e.code) from None


# --------------------------------------------------------------------------- #
# containers
# --------------------------------------------------------------------------- #
class Container:
    """Thin wrapper for running commands in one named container."""

    def __init__(self, cli, name):
        self.cli, self.name = cli, name

    def launch(self, image, ephemeral):
        run([self.cli, "launch", image, self.name]
            + (["--ephemeral"] if ephemeral else []))

    def delete(self):
        run([self.cli, "delete", "--force", self.name], check=False, capture=True)

    def root(self, script, **kw):
        return run([self.cli, "exec", self.name, "--", "bash", "-c", script], **kw)

    def root_argv(self, argv, env=None, cwd=None, input=None, capture=True):
        """Run argv as root with no shell involved (safe for untrusted strings).

        env goes on the command line, so never put secrets in it.
        """
        cmd = [self.cli, "exec", self.name]
        for k, v in (env or {}).items():
            cmd += ["--env", f"{k}={v}"]
        if cwd:
            cmd += ["--cwd", cwd]
        return subprocess.run(cmd + ["--", *argv], check=True, input=input,
                              capture_output=capture)

    def put_file(self, path, data, mode="0644"):
        """Write bytes to path in the container (as root) via stdin."""
        self.root_argv(["install", "-m", mode, "/dev/stdin", path], input=data)

    def user_cmd(self, *argv, env=None):
        cmd = [self.cli, "exec", self.name,
               "--user", UID, "--group", UID, "--cwd", HOME,
               "--env", f"HOME={HOME}", "--env", f"USER={USER}"]
        for k, v in (env or {}).items():
            cmd += ["--env", f"{k}={v}"]
        return cmd + ["--", *argv]

    def user(self, script, env=None, **kw):
        return run(self.user_cmd("bash", "-lc", script, env=env), **kw)

    def wait_network(self):
        log("waiting for network")
        self.root("command -v cloud-init >/dev/null && "
                  "cloud-init status --wait >/dev/null 2>&1 || true",
                  check=False)
        for _ in range(60):
            if self.root("getent hosts github.com >/dev/null",
                         check=False).returncode == 0:
                return
            time.sleep(1)
        raise RuntimeError("container never got network access")


# --------------------------------------------------------------------------- #
# cached base image
# --------------------------------------------------------------------------- #
def base_alias(source):
    h = hashlib.sha256("\0".join(
        (source, BASE_ROOT_SETUP, CLAUDE_INSTALL, BASE_CLEANUP)).encode())
    return IMAGE_PREFIX + h.hexdigest()[:12]


def cached_images(cli):
    """{alias: created_at (aware datetime)} for our local images."""
    out = run([cli, "image", "list", "local:", "--format", "json"],
              capture=True).stdout
    found = {}
    for img in json.loads(out or "[]"):
        # e.g. 2026-11-14T23:15:00.123456789Z; drop the fraction to stay
        # portable across Python versions.
        created = datetime.datetime.strptime(
            img["created_at"][:19], "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=datetime.timezone.utc)
        for a in img.get("aliases") or []:
            if a["name"].startswith(IMAGE_PREFIX):
                found[a["name"]] = created
    return found


def ensure_base_image(cli, source, max_age_days, rebuild):
    alias = base_alias(source)
    images = cached_images(cli)

    created = images.get(alias)
    if created and not rebuild:
        age = datetime.datetime.now(datetime.timezone.utc) - created
        if age < datetime.timedelta(days=max_age_days):
            log(f"using cached image {alias} "
                f"(built {age.days}d {age.seconds // 3600}h ago)")
            return alias
        log(f"cached image {alias} is older than {max_age_days}d, rebuilding")

    log(f"building base image {alias} from {source} (one-time)")
    build = Container(cli, f"{IMAGE_PREFIX}build-{uuid.uuid4().hex[:8]}")
    try:
        build.launch(source, ephemeral=False)
        build.wait_network()
        log("installing packages")
        build.root(BASE_ROOT_SETUP, capture=True)
        log("installing Claude Code")
        build.user(CLAUDE_INSTALL, capture=True)
        build.root(BASE_CLEANUP, check=False, capture=True)
        run([cli, "stop", build.name], capture=True)
        if alias in images:
            # Only affects future launches; running containers are unaffected.
            run([cli, "image", "delete", alias], check=False, capture=True)
        log("publishing image")
        run([cli, "publish", build.name, "--alias", alias], capture=True)
    finally:
        build.delete()

    # Drop images from older recipes / other source images.
    for stale in images:
        if stale != alias:
            run([cli, "image", "delete", stale], check=False, capture=True)
            log(f"removed stale image {stale}")
    return alias


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


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
class Sandbox:
    def __init__(self, args):
        self.args = args
        self.cli = args.cli
        self.name = args.name or f"claude-{uuid.uuid4().hex[:8]}"
        self.box = Container(self.cli, self.name)
        self.root, self.user, self.user_cmd = (
            self.box.root, self.box.user, self.box.user_cmd)
        self.key_id = None
        self.container_up = False
        self._torn_down = False
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

        # No-ops when launched from the cached image.
        self.root("command -v git >/dev/null && command -v curl >/dev/null || "
                  "(apt-get update -qq && "
                  "apt-get install -y -qq git curl openssh-client)",
                  capture=True)

        if self.args.tmux:
            self.root("command -v tmux >/dev/null || "
                      "(apt-get update -qq && apt-get install -y -qq tmux)",
                      capture=True)
            self.user("cat > ~/.tmux.conf", input=TMUX_CONF)

    def install_deps(self):
        """Install the repo's claude_sandbox_deps.yaml into the container.

        Runs before the SSH key and Claude credentials exist in the container,
        so installers never see them. Everything is parsed, downloaded and
        checksum-verified on the host; the container only receives bytes.
        Any problem aborts the launch (fail closed); --no-deps skips this.
        """
        f = sandbox_deps.DEPS_FILE
        if not self.args.repo:
            return
        if self.args.no_deps:
            self.deps_note = f"Skipped (`--no-deps`): `{f}` was not applied."
            return
        try:
            raw = gh_api("GET", f"/repos/{self.args.repo}/contents/{f}",
                         self.token, raw=True)
        except GitHubError as e:
            if e.code != 404:
                raise
            self.deps_note = f"This repo has no `{f}`, so nothing extra was installed."
            return
        deps = sandbox_deps.parse(raw)
        lines = sandbox_deps.summary(deps)
        self.deps_note = (f"Installed at launch from `{f}`:\n"
                          + "\n".join(f"  - {l}" for l in lines)
                          if lines else f"`{f}` is present but empty.")
        for l in lines:
            log(f"deps: {l}")

        box = self.box
        if deps["apt"]:
            log("installing apt packages")
            env = {"DEBIAN_FRONTEND": "noninteractive"}
            box.root_argv(["apt-get", "update", "-qq"], env=env)
            box.root_argv(["apt-get", "install", "-y", "-qq",
                           "--no-install-recommends", *deps["apt"]], env=env)

        for b in deps["binaries"]:
            log(f"installing binary {b['name']}")
            data = sandbox_deps.fetch(b["url"], sandbox_deps.MAX_BINARY, b["sha256"])
            if b["archive_path"]:
                data = sandbox_deps.extract_member(data, b["archive_path"])
            box.put_file(f"/usr/local/bin/{b['name']}", data, "0755")

        if deps["scripts"]:
            scratch = "/var/tmp/sandbox-deps"
            box.root_argv(["install", "-d", "-m", "0700", scratch])
            try:
                for s in deps["scripts"]:
                    log(f"running install script {s['name']}"
                        + ("" if s["sha256"] else " (no sha256 pinned!)"))
                    data = sandbox_deps.fetch(s["url"], sandbox_deps.MAX_SCRIPT,
                                              s["sha256"])
                    path = f"{scratch}/{s['name']}.sh"
                    box.put_file(path, data, "0700")
                    box.root_argv(["bash", path, *s["args"]], cwd=scratch,
                                  env=s["env"])
            finally:
                box.root_argv(["rm", "-rf", scratch], capture=True)

    def provision_key(self):
        log("generating SSH key inside the container")
        self.user(f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                  f"ssh-keygen -q -t ed25519 -N '' "
                  f"-C '{self.name}' -f ~/.ssh/id_ed25519")
        pubkey = self.user("cat ~/.ssh/id_ed25519.pub",
                           capture=True).stdout.strip()

        # Pin GitHub host keys from the API (over the host's TLS) instead of
        # trusting ssh-keyscan on first use.
        meta = gh_api("GET", "/meta", self.token)
        known = "".join(f"github.com {k}\n" for k in meta["ssh_keys"])
        self.user("cat >> ~/.ssh/known_hosts", input=known)

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

        self.user('git config --global url."git@github.com:".insteadOf '
                  '"https://github.com/"')
        if self.args.git_name:
            self.user(f"git config --global user.name '{self.args.git_name}'")
        if self.args.git_email:
            self.user(f"git config --global user.email '{self.args.git_email}'")
        # Linear history: rebase on pull, refuse non-fast-forward merges.
        self.user("git config --global pull.rebase true && "
                  "git config --global rebase.autoStash true && "
                  "git config --global merge.ff only && "
                  "git config --global push.autoSetupRemote true")

    def install_claude(self):
        if self.user("command -v claude >/dev/null", check=False).returncode == 0:
            return  # baked into the cached image
        log("installing Claude Code")
        self.user(CLAUDE_INSTALL, capture=True)

    def clone(self):
        if not self.args.repo:
            return HOME
        repo_dir = f"{HOME}/{self.args.repo.split('/')[1]}"
        log(f"cloning {self.args.repo}")
        self.user(f"git clone git@github.com:{self.args.repo}.git {repo_dir}")
        return repo_dir

    def write_claude_md(self, workdir):
        """Render the CLAUDE.md template into ~/.claude/ in the container.

        The template is plain Markdown with string.Template placeholders
        ($container, $access, $user, $uid, $home, $sudo, $tools, $branch, $deps,
        $deps_file);
        write $$ for a literal dollar sign.
        """
        probe = self.user(
            "sudo -n true 2>/dev/null && echo sudo=yes || echo sudo=no; "
            "printf 'tools='; for t in " + " ".join(SANDBOX_TOOLS) + "; do "
            "command -v $t >/dev/null && printf '%s ' $t; done; echo; "
            f"printf 'branch='; git -C '{workdir}' rev-parse --abbrev-ref HEAD "
            "2>/dev/null || echo",
            capture=True, check=False).stdout
        facts = dict(l.split("=", 1) for l in probe.splitlines() if "=" in l)
        tools = facts.get("tools", "").split()
        branch = facts.get("branch", "").strip()
        repo = self.args.repo

        if repo:
            access = [
                f"- Working copy: `{workdir}`, a clone of `github.com/{repo}`"
                + (f" (branch `{branch}`)" if branch else "") + ".",
                f"- GitHub access is an SSH **deploy key scoped to `{repo}` "
                "only**. Other repos can't be cloned or pushed over SSH. "
                "`https://github.com/` URLs are rewritten to SSH.",
                "- The deploy key is **read-only**: you cannot push. Tell the "
                "user early that commits will be lost when the sandbox ends "
                "unless they export them some other way."
                if self.args.read_only else
                "- The deploy key has write access, so you can push.",
            ]
        else:
            access = [f"- Working directory: `{workdir}`. No repo was cloned.",
                      "- An SSH key is registered on the user's GitHub "
                      "**account** for this session, so you can clone and "
                      "push to any repo they can access."]

        with open(self.args.claude_md) as f:
            template = string.Template(f.read())
        md = template.substitute(
            container=self.name,
            access="\n".join(access),
            user=USER, uid=UID, home=HOME,
            sudo=("Passwordless `sudo` works, so installing apt packages is "
                  "fine." if facts.get("sudo") == "yes" else
                  "No passwordless `sudo`, so you can't install system "
                  "packages."),
            tools=", ".join(f"`{t}`" for t in tools) or "(minimal image)",
            branch=f" (`{branch}`)" if branch else "",
            deps=self.deps_note,
            deps_file=sandbox_deps.DEPS_FILE,
        )
        self.user("mkdir -p ~/.claude && cat > ~/.claude/CLAUDE.md", input=md)

    def claude_secrets(self):
        secrets = {}
        token = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                 or read_token_file(self.args.token_file))
        if token:
            secrets["CLAUDE_CODE_OAUTH_TOKEN"] = token
        if os.environ.get("ANTHROPIC_API_KEY"):
            secrets["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        return secrets

    def push_secrets(self):
        """Write credentials to a 0600 file in the container via stdin.

        `incus exec --env K=V` would put them on the host's process list.
        Returns a shell prefix that loads and removes the file.
        """
        secrets = self.claude_secrets()
        if not secrets:
            log("no Claude credential found; run /login in the session "
                "(or create one with --setup-token)")
            return ""
        log("passing " + " and ".join(secrets) + " into the sandbox")
        self.user(f"umask 077 && cat > {SECRETS_FILE}", input="".join(
            f"export {k}={shlex.quote(v)}\n" for k, v in secrets.items()))
        # Already logged in, so skip Claude Code's first-run onboarding.
        self.user("[ -e ~/.claude.json ] || "
                  "echo '{\"hasCompletedOnboarding\": true}' > ~/.claude.json")
        return f". {SECRETS_FILE} && rm -f {SECRETS_FILE} && "

    def session(self, workdir):
        env = {k: os.environ[k] for k in TERM_ENV_VARS if os.environ.get(k)}
        load_secrets = self.push_secrets()
        prog = "bash -l" if self.args.shell else "claude"
        if self.args.tmux:
            # tmux sets its own TERM/TERM_PROGRAM, giving Claude Code a
            # well-known terminal; scroll with the mouse or prefix-[ .
            prog = f"tmux new-session -s claude '{prog}'"
        prog = f"exec {prog}"
        cmd = self.user_cmd("bash", "-lc",
                            f"{load_secrets}cd '{workdir}' && {prog}", env=env)
        cmd[cmd.index("--cwd") + 1] = workdir

        log("starting session (exit Claude Code to tear everything down)")
        # A no-op handler (not SIG_IGN) so Ctrl-C reaches Claude Code without
        # killing this script; handlers reset to default in the child on exec.
        prev = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            subprocess.run(cmd)
        finally:
            signal.signal(signal.SIGINT, prev)

    def setup_token(self):
        """Run `claude setup-token` interactively, then save the pasted token."""
        env = {k: os.environ[k] for k in TERM_ENV_VARS if os.environ.get(k)}
        log("running `claude setup-token`: sign in in your browser, then "
            "copy the token it prints")
        prev = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            subprocess.run(self.user_cmd("bash", "-lc", "claude setup-token",
                                         env=env))
        finally:
            signal.signal(signal.SIGINT, prev)
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
def main():
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
                   help="rebuild the cached base image even if it is fresh")
    p.add_argument("--max-age", type=float, default=7, metavar="DAYS",
                   help="rebuild the cached image when older than this "
                        "(default: 7)")
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
    args = p.parse_args()
    if not os.path.isfile(args.claude_md):
        p.error(f"--claude-md: no such file: {args.claude_md}")

    box = Sandbox(args)
    if not args.setup_token:
        box.token  # fail fast, before building or launching anything

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    try:
        image = args.image
        if not args.no_cache:
            image = ensure_base_image(args.cli, args.image,
                                      args.max_age, args.rebuild)
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
