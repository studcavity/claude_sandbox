"""sandbox.py - `Sandbox`: one container's lifecycle (create, secrets, clone, session, teardown)."""

import getpass
import os
import shlex
import threading
import subprocess
import sys
from claude_sandbox import claude_config
from claude_sandbox import provenance
from claude_sandbox import sessions
from claude_sandbox import timing
from claude_sandbox.container import deps as container_deps
from claude_sandbox.container import host
from claude_sandbox.container import mounts
from claude_sandbox.container import volumes
from claude_sandbox import paths
from claude_sandbox.container.incus import Container
from claude_sandbox.container.images import apply_deps
from claude_sandbox.util import log, sigint_to_child
from claude_sandbox.backends.github import GitHubBackend
from claude_sandbox.backends.local import LocalBackend
from claude_sandbox import mergecheck
from claude_sandbox import options
from claude_sandbox import credentials
from claude_sandbox import render
from claude_sandbox import resume
from claude_sandbox import session_tool
from claude_sandbox import shell


# `incus/lxc exec` starts with a clean environment: only TERM is carried over
# from the client. Without the rest, Claude Code can't identify the outer
# terminal (or that it's running under tmux/screen) and falls back to
# conservative rendering, which can break native scrollback. Forward the
# terminal-identity variables so the session behaves like it does on the host.
# (LANG/LC_ALL are deliberately not forwarded: the host locale may not exist
# in the container; incus already sets LANG=C.UTF-8.)
TERM_ENV_VARS = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
                 "LC_TERMINAL", "LC_TERMINAL_VERSION")


# Tools listed in the sandbox's CLAUDE.md when present.
SANDBOX_TOOLS = ("git", "python3", "pip3", "rg", "fd", "jq", "tree", "gcc",
                 "make", "gdb", "strace", "lsof", "htop", "dig", "nc", "tmux",
                 "curl", "wget")


def choose_image(flag, repo_image):
    """(image, origin) for the base OS: --image beats the repo's `image:` in
    claude_sandbox_deps.yaml, which beats the default."""
    if flag:
        return flag, "--image"
    if repo_image:
        return repo_image, container_deps.DEPS_FILE
    return options.DEFAULT_IMAGE, "default"


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


class Sandbox:
    def __init__(self, args, backend=None):
        self.args = args
        self.cli = args.cli
        self.backend = backend or make_backend(args)
        # The container name doubles as the session id (key title, branches).
        # Never reuse a name whose signing key is recorded for this repo: the
        # new key would vouch for the old session's branches.
        self.name = args.name or sessions.pick_name(
            Container.list_names(self.cli)
            | provenance.recorded_sessions(self.backend.key_repo,
                                                   self.backend.key_root))
        self.session_id = sessions.validate_session(self.name)
        self.box = Container(self.cli, self.name)
        self.container_up = False
        self._torn_down = False
        self._volumes_thread = None   # see teardown() / wait_volumes()
        self._release_kept = False    # land() said a kept volume's work is safe
        self.deps = None
        self.deps_baked = False
        self.image = None
        self.usb_devices = []
        self.volumes = volumes.Volumes(self.cli, self.box, [], None)
        self.deps_note = ("No repo was cloned, so there is no "
                          f"`{container_deps.DEPS_FILE}`.")

    def launch(self, image):
        log(f"launching ephemeral container {self.name} ({image})")
        with timing.stage("incus launch"):
            self.box.launch(image, ephemeral=True, config=launch_config(self.args),
                            root_size=self.args.disk_size)
        self.container_up = True
        with timing.stage("wait for network"):
            self.box.wait_network()
        with timing.stage("self-check + marker + package probe"):
            self.box.self_check()  # fail fast, before any real work
            # The marker lets repo scripts refuse to run anywhere but inside a
            # sandbox; the package probe is a no-op from the cached image.
            # One exec for both.
            self.box.root(shell.marker_script(self.name) + "; "
                          "command -v git >/dev/null && command -v curl >/dev/null || "
                          "(apt-get update -qq && "
                          "apt-get install -y -qq git curl openssh-client)",
                          capture=True)

        if self.args.tmux:
            self.box.root("command -v tmux >/dev/null || "
                          "(apt-get update -qq && apt-get install -y -qq tmux)",
                          capture=True)
            self.box.user("cat > ~/.tmux.conf", input=shell.TMUX_CONF)

    def attach_volumes(self):
        """Mount size-capped volumes over /tmp and the clone directory."""
        mounts = volumes.plan(self.name, self.backend.workdir(),
                                      self.args.volume_size,
                                      keep_work=self.backend.keep_work(
                                          self.session_id))
        self.volumes = volumes.Volumes(
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
        f = container_deps.DEPS_FILE
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
        deps = container_deps.parse(raw)
        lines = container_deps.summary(deps)
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
        raw = self.backend.read_file(host.HOST_FILE)
        if raw is not None:
            requests = host.parse(raw)["usb"]
        try:
            allow = host.load_allowlist(a.usb_allow)
        except ValueError as e:
            raise RuntimeError(f"USB allowlist: {e}") from None
        self.usb_devices = host.resolve_usb(
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

    def attach_mounts(self, workdir):
        """Bind-mount the --mount directories (host-granted only). Same slot
        as USB: after install_deps, before any credential exists."""
        mnts = mounts.check_set(self.args.mount, workdir)
        for i, m in enumerate(mnts):
            log(f"mount: {m.source} -> {m.path} ({m.mode})")
            self.box.add_mount(i, m.source, m.path, m.readonly)

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
            r = self.box.user_argv(shell.setup_argv(s), env=s["env"], cwd=workdir,
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
            + shell.git_config_script(self.args.git_name, self.args.git_email,
                                github_ssh=bool(known))
            + (" && " + provenance.container_script() if signing else ""),
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
            if provenance.save_key(self.backend.key_repo, self.session_id,
                                           (out.strip().splitlines() or [""])[0],
                                           root=self.backend.key_root):
                log(f"warning: replaced the recorded signing key of an earlier "
                    f"session named {self.session_id}; its unmerged PRs will "
                    "fail the provenance check")
        except (OSError, ValueError) as e:
            log(f"warning: couldn't record the session's signing key ({e}); "
                f"its PRs will fail {provenance.CONTEXT}")

    def install_claude(self):
        if self.box.user("command -v claude >/dev/null", check=False).returncode == 0:
            return  # baked into the cached image
        log("installing Claude Code")
        self.box.user(shell.CLAUDE_INSTALL, capture=True)

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
        self.box.user("mkdir -p " + shlex.quote(session_tool.SESSION_TOOL_DIR) + " "
                      + shlex.quote(os.path.dirname(session_tool.SESSION_TOOL_BIN)))
        self.box.root_argv(["tar", "-xzf", "-", "-C", "/"],
                           input=session_tool.session_tool_archive(session_tool.session_tool_files(paths.ROOT)))

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
        md = render.render_claude_md(template, render.parse_probe(probe),
                              container=self.name, workdir=workdir,
                              repo=self.backend.label,
                              read_only=self.args.read_only,
                              access=self.backend.access_lines(
                                  workdir, render.parse_probe(probe)["branch"]),
                              kind=self.backend.kind,
                              deps_note=self.deps_note,
                              session=self.session_id,
                              usb_devices=self.usb_devices, cli=self.cli,
                              mounts=self.args.mount,
                              disk_note=self.disk_note())
        self.box.user("mkdir -p ~/.claude/ref && cat > ~/.claude/CLAUDE.md",
                      input=md)
        with open(os.path.join(paths.DATA, render.DEPS_REF_TEMPLATE)) as f:
            ref = render.render_deps_ref(f.read())
        self.box.user(f"cat > {render.DEPS_REF_PATH}", input=ref)

    def seed_claude(self, workdir, secrets):
        """Pre-answer Claude Code's first-run dialogs (see
        claude_config). Never blocks the launch."""
        state = claude_config.claude_state(
            workdir, secrets, self.backend.trusted())
        r = self.box.user_argv(["python3", "-c",
                                claude_config.MERGE_SCRIPT],
                               input=claude_config.merge_input(state),
                               capture=True, check=False)
        if r.returncode != 0:
            log("warning: couldn't pre-answer Claude Code's first-run "
                f"dialogs: {(r.stderr or '').strip()[-200:]}")

    def push_secrets(self, workdir):
        """Write credentials to a 0600 file in the container via stdin.

        `incus exec --env K=V` would put them on the host's process list.
        Returns a shell prefix that loads and removes the file.
        """
        secrets = credentials.claude_secrets(os.environ, self.args.token_file)
        self.seed_claude(workdir, secrets)
        if not secrets:
            log("no Claude credential found; run /login in the session "
                "(or create one with --setup-token)")
            return ""
        log("passing " + " and ".join(secrets) + " into the sandbox")
        self.box.user(f"umask 077 && cat > {shell.SECRETS_FILE}",
                      input=shell.secrets_script(secrets))
        return f". {shell.SECRETS_FILE} && rm -f {shell.SECRETS_FILE} && "

    def session(self, workdir):
        env = {k: os.environ[k] for k in TERM_ENV_VARS if os.environ.get(k)}
        prompt = None
        while True:
            script = shell.session_script(workdir, self.push_secrets(workdir),
                                    shell=self.args.shell,
                                    model=self.args.model,
                                    tmux=self.args.tmux, resume_prompt=prompt)
            cmd = self.box.user_cmd("bash", "-lc", script, env=env)
            cmd[cmd.index("--cwd") + 1] = workdir

            log("starting session (exit Claude Code to tear everything down)"
                if prompt is None else
                "resuming the session with the final check's failures")
            timing.mark("prompt")
            with sigint_to_child():
                subprocess.run(cmd)
            prompt = self.final_check(workdir)
            if prompt is None:
                return

    def merge_findings(self, workdir):
        """Branches of this session that won't land cleanly on the base (see
        mergecheck), run in the container. [] when all is well or there is
        nothing to land; never raises."""
        if not self.backend.has_commits:
            return []

        def git(*args):
            argv = ["git", *args]
            if args[0] == "fetch":
                argv = ["timeout", mergecheck.FETCH_TIMEOUT, *argv]
            r = self.box.user_argv(argv, cwd=workdir, capture=True,
                                   check=False)
            return r.returncode, r.stdout or ""
        try:
            findings, notes = mergecheck.inspect(
                git, self.session_id, remote=self.backend.can_push)
        except Exception as e:  # a broken check must not trap the session
            log(f"warning: couldn't check the branches' mergeability: {e}")
            return []
        for n in notes:
            log(f"mergeability: {n}")
        return findings

    def final_check(self, workdir):
        """Run the repo's `check:` scripts, and the launcher's own
        mergeability check of the session's branches, now that Claude has
        exited, while the container and its deploy key are still alive.
        Returns the prompt to resume Claude with when something failed and the
        user wants that, else None. Ctrl-C skips the check (and tears down)."""
        a = self.args
        checks = (self.deps or {}).get("check") or []
        if a.shell or a.no_final_check:
            return None
        failures = []
        for s in checks:
            log(f"final check: running {s['path']} (timeout {s['timeout']}s; "
                "Ctrl-C skips it and tears down)")
            cmd = self.box.user_cmd(*shell.setup_argv(s), env=s["env"])
            cmd[cmd.index("--cwd") + 1] = workdir
            code, out = resume.run_streaming(cmd)
            if code != 0:
                failures.append((s, code, out))
        merge = self.merge_findings(workdir)
        if not failures and not merge:
            if checks:
                log("final check passed")
            return None
        names = [s["path"] for s, _, _ in failures]
        if merge:
            names.append("mergeability: "
                         + ", ".join(sorted({b for b, _ in merge})))
        log(f"final check FAILED: {', '.join(names)}")
        if not resume.ask_resume(names):
            log("not resuming: tearing down with the check failing (what "
                + ("was pushed still becomes a PR" if self.backend.can_push
                   else "was committed is still imported")
                + "; say so in review)")
            return None
        return resume.resume_prompt(failures, can_push=self.backend.can_push,
                                    merge_findings=merge)

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
        credentials.save_token_file(self.args.token_file, token)
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


def ask_usb(req):
    """Interactive approval of one repo-requested device (default: no)."""
    print(f"\nThe repo requests USB device "
          f"\033[1m{req['vendor_id']}:{req['product_id']}\033[0m "
          f"(name {req['name']!r}).\nIts description (from the repo, "
          f"untrusted): {host.display_text(req['description'])}\n"
          "Attach it to the sandbox? Only approve hardware you own and trust.",
          file=sys.stderr)
    try:
        return input("Attach? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def make_backend(args):
    """The Backend for these arguments (see backends_base)."""
    if getattr(args, "local", None):
        return LocalBackend(args)
    return GitHubBackend(args)
