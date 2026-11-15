"""container/incus.py - thin wrapper around `lxc`/`incus` for one container."""
import re
import subprocess
import time

from claude_sandbox.util import log, run

USER, UID, HOME = "ubuntu", "1000", "/home/ubuntu"
HEX4 = re.compile(r"^[0-9a-f]{4}$")


class Container:
    """Thin wrapper for running commands in one named container."""

    def __init__(self, cli, name):
        self.cli, self.name = cli, name

    @staticmethod
    def list_names(cli):
        """Names of every container (any state) known to the runtime."""
        out = run([cli, "list", "--format", "csv", "-c", "n"],
                  capture=True).stdout
        return {l.strip() for l in out.splitlines() if l.strip()}

    def launch(self, image, ephemeral, config=None, root_size=None):
        """root_size (e.g. "30GiB") caps the root disk with a storage quota."""
        cfg = [a for k, v in (config or {}).items()
               for a in ("-c", f"{k}={v}")]
        dev = ["--device", f"root,size={root_size}"] if root_size else []
        run([self.cli, "launch", image, self.name]
            + (["--ephemeral"] if ephemeral else []) + cfg + dev)

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
        """Write bytes to path in the container (as root) via stdin.

        Reads fd 0 through the shell only: `incus exec` stdin can't be
        reopened as /dev/stdin (so no `install ... /dev/stdin`). The data goes
        to a private temp file that is chmod'ed and then renamed, so a partial
        write is never readable or executable at the final path. path and mode
        are positional args, never interpolated into the script.
        """
        self.root_argv(
            ["sh", "-c",
             'umask 077 && cat > "$1.tmp" && chmod "$2" "$1.tmp" '
             '&& mv "$1.tmp" "$1"',
             "sh", path, mode], input=data)

    def add_usb(self, vid, pid):
        """Pass host USB devices matching vid:pid into the container.

        Runs on the host, no shell. Owned by the session user with mode 0660
        (never world-accessible). Incus attaches every host device with this
        id, and re-attaches after it re-enumerates. IDs are re-validated here
        so a caller bug can't smuggle extra device options.
        """
        if not (HEX4.match(vid) and HEX4.match(pid)):
            raise ValueError(f"bad USB id {vid!r}:{pid!r}")
        run([self.cli, "config", "device", "add", self.name,
             f"usb-{vid}-{pid}", "usb", f"vendorid={vid}", f"productid={pid}",
             f"uid={UID}", f"gid={UID}", "mode=0660"])

    def self_check(self):
        """Round-trip a file through put_file; raises if the container's exec
        and stdin plumbing can't do what image/deps installs rely on."""
        path = "/tmp/.claude-sandbox-canary"
        self.put_file(path, b"ok", "0600")
        out = self.root_argv(["cat", path]).stdout
        self.root_argv(["rm", "-f", path])
        if out != b"ok":
            raise RuntimeError(f"container self-check failed: read back {out!r}")

    def user_cmd(self, *argv, env=None):
        cmd = [self.cli, "exec", self.name,
               "--user", UID, "--group", UID, "--cwd", HOME,
               "--env", f"HOME={HOME}", "--env", f"USER={USER}"]
        for k, v in (env or {}).items():
            cmd += ["--env", f"{k}={v}"]
        return cmd + ["--", *argv]

    def user_argv(self, argv, env=None, cwd=None, **kw):
        """Run argv as the session user with no shell interpreting it."""
        cmd = self.user_cmd(*argv, env=env)
        if cwd:
            cmd[cmd.index("--cwd") + 1] = cwd
        return run(cmd, **kw)

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
