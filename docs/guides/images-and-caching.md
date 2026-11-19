---
type: Guide
title: "Cached images and disk space"
description: "How base and deps images are built, refreshed and kept, and how disk quotas keep one sandbox from filling the host pool."
tags: [images, disk, incus]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: images
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/container/images.py
    title: Cached base and deps images
  - id: pool
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/container/pool.py
    title: Storage pool free-space check
  - id: volumes
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/container/volumes.py
    title: Size-capped volumes
---

# Cached base image

On first run the launcher builds a local image (`claude-sandbox-<hash>`) with
Claude Code, git, ssh, tmux, Python, build tools and common debugging tools;
later sandboxes launch from it.[^images] The image holds no credentials: SSH keys
and git identity are made per session. It is rebuilt when:

- `--image` or the build steps change (both feed the hash), or you pass
  `--rebuild`;
- it is older than `--hard-max-age` days (default 30). Between `--max-age`
  (default 7) and that it is still used while a detached process refreshes it,
  so a launch never waits for a routine refresh.

The newest 4 base images (one per OS image in use) are kept; older ones are
deleted after a rebuild. `--no-cache` skips the cache. A ZFS or btrfs storage
pool makes launching a copy-on-write clone instead of an unpack. A repo's own
tools are baked into a second, per-repo image; see
[The deps file](../reference/repo-deps-file.md).

# Disk space

Every sandbox, image build and cached image shares one Incus storage pool. A
default loop-backed pool is small (10 GiB), so a few deps images can fill it and
break launches for everyone. The launcher therefore:[^pool]

- checks the pool before building or launching and stops with the exact
  `incus storage set <pool> size=50GiB` command when under 4 GiB is free
  (`--grow-pool SIZE` runs it for you; it changes the host, so it is opt-in);
- caps the root disk of sandboxes and image builds (`--disk-size`, default
  30 GiB) and mounts separate capped volumes over `/tmp` and the clone directory
  (`--volume-size`, default 20 GiB), so a runaway build fills its own volume.
  They are deleted at exit, and ones leaked by a crashed launcher are swept on
  the next launch. `0` disables either; quotas need a pool driver that supports
  them (btrfs, zfs, lvm);
- keeps at most 4 deps images, and caps the journal at 100 MB in the base image.

[^images]: Cached base and deps images
[^pool]: Storage pool free-space check
