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
  5. On exit (normal, error, Ctrl-C, SIGTERM, SIGHUP) deletes the GitHub key
     and destroys the container.

GitHub auth: set GITHUB_TOKEN / GH_TOKEN, or be logged in with `gh`.
  - Account key mode needs the `admin:public_key` scope
    (gh auth refresh -s admin:public_key).
  - Deploy key mode needs admin rights on the repo
    (fine-grained token: "Administration: read & write").

Claude auth: if ANTHROPIC_API_KEY is set on the host it is passed into the
session. Otherwise, run /login inside Claude Code.

Examples:
  ./claude_sandbox.py --repo myuser/myproject      # deploy key + clone
  ./claude_sandbox.py                              # account-wide key
  ./claude_sandbox.py --cli incus --image images:ubuntu/24.04 --repo me/proj
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

API = "https://api.github.com"
USER, UID, HOME = "ubuntu", "1000", "/home/ubuntu"


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


def gh_api(method, path, token, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub {method} {path} -> {e.code}: "
                           f"{e.read().decode(errors='replace')}") from None


# --------------------------------------------------------------------------- #
# sandbox
# --------------------------------------------------------------------------- #
class Sandbox:
    def __init__(self, args):
        self.args = args
        self.cli = args.cli
        self.name = args.name or f"claude-{uuid.uuid4().hex[:8]}"
        self.token = github_token()
        self.key_id = None
        self.container_up = False
        self._torn_down = False

    # -- container exec -------------------------------------------------------
    def root(self, script, **kw):
        return run([self.cli, "exec", self.name, "--", "bash", "-c", script], **kw)

    def user_cmd(self, *argv, env=None):
        cmd = [self.cli, "exec", self.name,
               "--user", UID, "--group", UID, "--cwd", HOME,
               "--env", f"HOME={HOME}", "--env", f"USER={USER}"]
        for k, v in (env or {}).items():
            cmd += ["--env", f"{k}={v}"]
        return cmd + ["--", *argv]

    def user(self, script, env=None, **kw):
        return run(self.user_cmd("bash", "-lc", script, env=env), **kw)

    # -- lifecycle ------------------------------------------------------------
    def launch(self):
        log(f"launching ephemeral container {self.name} ({self.args.image})")
        run([self.cli, "launch", self.args.image, self.name, "--ephemeral"])
        self.container_up = True

        log("waiting for network")
        self.root("command -v cloud-init >/dev/null && "
                  "cloud-init status --wait >/dev/null 2>&1 || true",
                  check=False)
        for _ in range(60):
            if self.root("getent hosts github.com >/dev/null",
                         check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("container never got network access")

        self.root("command -v git >/dev/null && command -v curl >/dev/null || "
                  "(apt-get update -qq && "
                  "apt-get install -y -qq git curl openssh-client)",
                  capture=True)

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

    def install_claude(self):
        log("installing Claude Code")
        self.user("curl -fsSL https://claude.ai/install.sh | bash",
                  capture=True)

    def clone(self):
        if not self.args.repo:
            return HOME
        repo_dir = f"{HOME}/{self.args.repo.split('/')[1]}"
        log(f"cloning {self.args.repo}")
        self.user(f"git clone git@github.com:{self.args.repo}.git {repo_dir}")
        return repo_dir

    def session(self, workdir):
        env = {}
        if os.environ.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        prog = "exec bash -l" if self.args.shell else "exec claude"
        cmd = self.user_cmd("bash", "-lc", f"cd '{workdir}' && {prog}", env=env)
        cmd[cmd.index("--cwd") + 1] = workdir

        log("starting session (exit Claude Code to tear everything down)")
        # A no-op handler (not SIG_IGN) so Ctrl-C reaches Claude Code without
        # killing this script; handlers reset to default in the child on exec.
        prev = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            subprocess.run(cmd)
        finally:
            signal.signal(signal.SIGINT, prev)

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
            run([self.cli, "delete", "--force", self.name],
                check=False, capture=True)
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
    args = p.parse_args()

    box = Sandbox(args)

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    try:
        box.launch()
        box.provision_key()
        box.install_claude()
        box.session(box.clone())
    except KeyboardInterrupt:
        log("interrupted")
    except (subprocess.CalledProcessError, RuntimeError) as e:
        log(f"error: {e}")
        if getattr(e, "stderr", None):
            print(e.stderr, file=sys.stderr)
        return 1
    finally:
        box.teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
