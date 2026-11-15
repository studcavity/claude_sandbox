"""sandbox_volumes.py - size-capped storage volumes for one sandbox container.

Why separate volumes: a runaway build in the repo or /tmp must not fill the
root filesystem, which would break apt, ssh, git and Claude Code itself. Each
volume has its own quota, so one filling up only breaks that mount.

Why custom code: incus has no "ephemeral volume" concept (custom volumes
outlive their instance), so creation, attachment, deletion and the sweep of
volumes leaked by crashed launchers are ours. The decision logic (`plan`,
`orphans`) is pure; `Volumes` is the thin I/O layer over the incus CLI.
"""
import datetime
import json
import subprocess
import time
from typing import NamedTuple

from sandbox_util import log, run

VOLUME_PREFIX = "claude-sandbox-vol-"
ORPHAN_MIN_AGE = datetime.timedelta(hours=1)  # never race a launch in progress
DELETE_RETRIES = 15  # an ephemeral instance is removed asynchronously


class Mount(NamedTuple):
    volume: str  # storage volume name
    device: str  # instance device name
    path: str  # mount point inside the container
    mode: str  # chmod applied to the mount point
    owner: str  # chown applied to the mount point


def plan(container, workdir, size, user="ubuntu"):
    """Mounts to create for a container; none when size is falsy.

    /tmp is always covered; the clone directory is covered when there is one
    (a bare $HOME workdir is left on the root disk).
    """
    if not size:
        return []
    mounts = [Mount(f"{VOLUME_PREFIX}{container}-tmp", "cs-tmp", "/tmp",
                    "1777", "root:root")]
    if workdir and workdir.count("/") > 2:  # /home/<user>/<repo>, not $HOME
        mounts.append(Mount(f"{VOLUME_PREFIX}{container}-work", "cs-work",
                            workdir, "0755", f"{user}:{user}"))
    return mounts


def orphans(volumes_json, now, min_age=ORPHAN_MIN_AGE):
    """Names of our volumes that nothing uses and that are old enough to be
    leaked by a crashed launcher. Input: `volume list --format json`."""
    found = []
    for v in json.loads(volumes_json or "[]"):
        name = v.get("name", "")
        if v.get("type") != "custom" or not name.startswith(VOLUME_PREFIX):
            continue
        if v.get("used_by"):
            continue
        created = (v.get("created_at") or "")[:19]
        try:
            made = datetime.datetime.strptime(
                created, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            continue  # unknown age: leave it alone
        if now - made >= min_age:
            found.append(name)
    return found


class Volumes:
    """Create, attach and delete the volumes of one container."""

    def __init__(self, cli, box, mounts, size):
        self.cli, self.box, self.mounts, self.size = cli, box, mounts, size
        self.pool = None
        self.created = []

    def _discover_pool(self):
        out = run([self.cli, "profile", "device", "get", "default", "root",
                   "pool"], capture=True).stdout.strip()
        if not out:
            raise RuntimeError("could not find the default storage pool")
        return out

    def sweep(self, now=None):
        """Delete leaked volumes from earlier crashed launches."""
        self.pool = self.pool or self._discover_pool()
        out = run([self.cli, "storage", "volume", "list", self.pool,
                   "--format", "json"], capture=True).stdout
        now = now or datetime.datetime.now(datetime.timezone.utc)
        for name in orphans(out, now):
            run([self.cli, "storage", "volume", "delete", self.pool, name],
                check=False, capture=True)
            log(f"removed leaked volume {name}")

    def attach(self):
        """Create and mount every volume. Call after the container boots.

        Best effort: a pool that can't enforce quotas (e.g. plain `dir`) just
        means the volume isn't created, with a warning.
        """
        if not self.mounts:
            return
        try:
            self.sweep()
            for m in self.mounts:
                run([self.cli, "storage", "volume", "create", self.pool,
                     m.volume, f"size={self.size}"], capture=True)
                self.created.append(m.volume)
                run([self.cli, "config", "device", "add", self.box.name,
                     m.device, "disk", f"pool={self.pool}",
                     f"source={m.volume}", f"path={m.path}"], capture=True)
                self.box.root_argv(["chown", m.owner, m.path])
                self.box.root_argv(["chmod", m.mode, m.path])
                log(f"mounted {self.size} volume at {m.path}")
        except (subprocess.CalledProcessError, RuntimeError) as e:
            err = getattr(e, "stderr", None) or e
            log(f"warning: separate volumes unavailable ({str(err).strip()}); "
                "continuing with everything on the root disk")

    def delete(self):
        """Delete what attach() created; call after the container is gone."""
        for name in self.created:
            for _ in range(DELETE_RETRIES):
                r = run([self.cli, "storage", "volume", "delete", self.pool,
                         name], check=False, capture=True)
                if r.returncode == 0:
                    break
                time.sleep(1)
            else:
                log(f"WARNING: could not delete volume {name}; "
                    "the next launch will sweep it")
        self.created = []
