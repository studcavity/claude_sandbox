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
# A work volume that must outlive its container until the work on it has been
# copied out (the local backend). Never matched by the orphan sweep; deleted by
# release() once its work is safe, or by `--recover` after a crash.
KEEP_PREFIX = "claude-sandbox-keep-"
META_PREFIX = "user.claude-sandbox."   # incus user.* keys on a kept volume
ORPHAN_MIN_AGE = datetime.timedelta(hours=1)  # never race a launch in progress
DELETE_RETRIES = 15  # an ephemeral instance is removed asynchronously


class Mount(NamedTuple):
    volume: str  # storage volume name
    device: str  # instance device name
    path: str  # mount point inside the container
    mode: str  # chmod applied to the mount point
    owner: str  # chown applied to the mount point
    config: tuple = ()  # (key, value) pairs set on the volume when created
    keep: bool = False  # survives delete(); only release() removes it


def plan(container, workdir, size, user="ubuntu", keep_work=None):
    """Mounts to create for a container; none when size is falsy.

    /tmp is always covered; the clone directory is covered when there is one
    (a bare $HOME workdir is left on the root disk). `keep_work` (a dict of
    metadata) makes the clone directory's volume a kept one, labelled with
    that metadata as user.claude-sandbox.<key>.
    """
    if not size:
        return []
    mounts = [Mount(f"{VOLUME_PREFIX}{container}-tmp", "cs-tmp", "/tmp",
                    "1777", "root:root")]
    if workdir and workdir.count("/") > 2:  # /home/<user>/<repo>, not $HOME
        if keep_work is None:
            mounts.append(Mount(f"{VOLUME_PREFIX}{container}-work", "cs-work",
                                workdir, "0755", f"{user}:{user}"))
        else:
            meta = tuple(sorted((META_PREFIX + k, str(v))
                                for k, v in keep_work.items()))
            mounts.append(Mount(f"{KEEP_PREFIX}{container}-work", "cs-work",
                                workdir, "0755", f"{user}:{user}", meta, True))
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


def default_pool(cli):
    """The storage pool of the default profile's root disk."""
    out = run([cli, "profile", "device", "get", "default", "root",
               "pool"], capture=True).stdout.strip()
    if not out:
        raise RuntimeError("could not find the default storage pool")
    return out


class Volumes:
    """Create, attach and delete the volumes of one container."""

    def __init__(self, cli, box, mounts, size):
        self.cli, self.box, self.mounts, self.size = cli, box, mounts, size
        self.pool = None
        self.created = []

    def _discover_pool(self):
        return default_pool(self.cli)

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
                     m.volume, f"size={self.size}",
                     *[f"{k}={v}" for k, v in m.config]], capture=True)
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

    def kept(self):
        """Names of created volumes that delete() leaves in place."""
        keep = {m.volume for m in self.mounts if m.keep}
        return [n for n in self.created if n in keep]

    def delete(self):
        """Delete what attach() created, except kept volumes; call after the
        container is gone."""
        kept = self.kept()
        for name in self.created:
            if name not in kept:
                self._delete(name, "the next launch will sweep it")
        self.created = kept

    def release(self):
        """Delete the kept volumes too (their work is safe elsewhere)."""
        for name in self.kept():
            self._delete(name, f"delete it with `{self.cli} storage volume "
                               f"delete {self.pool} {name}`")
        self.created = []

    def _delete(self, name, fallback):
        for _ in range(DELETE_RETRIES):
            r = run([self.cli, "storage", "volume", "delete", self.pool,
                     name], check=False, capture=True)
            if r.returncode == 0:
                log(f"deleted volume {name}")
                return
            time.sleep(1)
        log(f"WARNING: could not delete volume {name}; {fallback}")
