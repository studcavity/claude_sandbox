# Use a btrfs (copy-on-write) storage pool for cached images

## Problem
Incus's default pool is the `dir` driver, which has no copy-on-write: every
`incus launch` from a cached base or deps image unpacks and copies the whole
rootfs (about 5 to 14s for a ~700MB deps image, measured in a nested sandbox),
and every container then holds its own full copy on disk. On btrfs (or zfs/lvm
thin) the launch is a snapshot of the image volume: near-instant and sharing
blocks.

## Not tested yet
This could not be tried in a sandbox: a file-backed btrfs pool needs loop
devices (`/dev/loop-control`), which the sandbox container does not have
(`mount -o loop` fails; creating the nodes was refused). The kernel does
support btrfs and `btrfs-progs` installs. It needs a real host.

## Next steps
1. On a host, `incus storage create t btrfs size=20GiB`, then time
   `incus launch <deps-image> x -s t` against the same launch on a `dir` pool.
2. Compare the full `./scripts/smoke.sh` and a cold/warm real launch on both.
3. If it wins, decide how the launcher gets there: document it as a host setup
   recommendation (an existing btrfs host is simplest), have the launcher use a
   named pool (`-s`) when present, or create one. Check the root-disk size
   handling in `container/incus.py` (`root_size`) still applies on btrfs.
4. Teach the integration tests / CI (`integration` job) whether to run on both.

## Related
`publish` already uses zstd compression (`container/images.py`
`publish_argv`), which cut publish time 2.7x (40s to 15s for ~700MB, measured); btrfs addresses the launch/copy cost
that remains.
