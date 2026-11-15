"""sandbox_pool.py - free-space check (and opt-in growth) of the incus pool.

Per-container quotas don't help when the *pool* is full: every image, build
container and sandbox shares it, and a default loop-backed pool is small (10GiB
here), so a few cached deps images can starve everything. This checks the pool
before a long build or launch and says exactly how to grow it. Pure decision
logic (`parse_space`, `assess`) plus two thin CLI calls.
"""
import subprocess

import yaml

from sandbox_util import log, run

GiB = 1024 ** 3
MIN_FREE = 4 * GiB   # below this a build or session will very likely fail
WARN_FREE = 12 * GiB


def parse_space(info_text):
    """(used, total) bytes from `storage info --bytes` output, else None.

    Pools without resource accounting (e.g. dir on a shared fs) omit these
    keys; then there is nothing to check.
    """
    try:
        info = yaml.safe_load(info_text) or {}
        return int(info["space used"]), int(info["total space"])
    except (KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        return None


def assess(space, min_free=MIN_FREE, warn_free=WARN_FREE):
    """'ok' | 'warn' | 'full' for (used, total); 'ok' when unknown."""
    if space is None:
        return "ok"
    free = space[1] - space[0]
    return "full" if free < min_free else "warn" if free < warn_free else "ok"


def default_pool(cli):
    return run([cli, "profile", "device", "get", "default", "root", "pool"],
               capture=True).stdout.strip()


def grow_command(cli, pool, size="50GiB"):
    return f"{cli} storage set {pool} size={size}"


def check(cli, grow_to=None):
    """Return an error message when the pool is too full to work in, else
    None (logging a warning when it is merely low). With grow_to, grow a
    loop-backed pool first. Never raises on CLI trouble: the check is advisory.
    """
    try:
        pool = default_pool(cli)
        if grow_to:
            log(f"growing storage pool {pool} to {grow_to}")
            run([cli, "storage", "set", pool, f"size={grow_to}"], capture=True)
        info = run([cli, "storage", "info", pool, "--bytes"],
                   capture=True).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"warning: couldn't check the storage pool ({e})")
        return None
    space = parse_space(info)
    state = assess(space)
    if state == "ok":
        return None
    free = (space[1] - space[0]) / GiB
    msg = (f"storage pool {pool!r} has only {free:.1f}GiB free of "
           f"{space[1] / GiB:.0f}GiB. Grow it on the host with "
           f"`{grow_command(cli, pool)}` (loop-backed pools), or relaunch "
           f"with --grow-pool 50GiB; images are shared by every sandbox")
    if state == "full":
        return msg
    log(f"warning: {msg}")
    return None
