---
type: Reference
title: "claude_sandbox_deps.yaml"
description: "Schema, caching, base-OS choice and trust rules for the file a repo uses to get tooling installed in its sandbox."
tags: [deps, config, image]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: deps
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/container/deps.py
    title: Parse, validate and download the deps file
  - id: ref
    resource: https://github.com/studcavity/claude_sandbox/blob/main/docs/session/common/deps-file.md
    title: The session-facing schema reference
---

A repo puts `claude_sandbox_deps.yaml` at the root of its default branch to have
tooling installed in the container before the session starts. The host fetches
it through the GitHub API, validates it against a strict schema, downloads
everything, and bakes the result into a cached image.[^deps] The session is
told how to edit it by `docs/session/common/deps-file.md`, shipped into its
container.

The format is the same page the session reads, so it lives in one place:
[Editing the deps file](../../session/common/deps-file.md).

Install order is apt, binaries, scripts. `--no-deps` skips all of it. Two
optional metadata keys, `last_generated` (quoted ISO 8601 UTC timestamp) and
`platform` (for example `lsb_release -a` plus `uname -m`), record where the
toolchain was resolved so it can be reproduced; they are validated but never
executed and do not affect caching. Scripts under `check:` are described in
[The final check](final-check.md).

# Caching

The deps are baked into a per-repo local image (`claude-sandboxdeps-<hash>`,
derived from the base image), so only the first launch after a change pays for
the installs. The key covers the base image and the validated
`apt`/`scripts`/`binaries` content; comments, metadata, `image:` and `setup:` do
not count. The image is refreshed on the same `--max-age` / `--hard-max-age`
rules as the [base image](../guides/images-and-caching.md), rebuilt when the base
was rebuilt after it or with `--rebuild`, and the newest 4 are kept. It is built
in a throwaway container with no credentials, from host-verified downloads. If
any script lacks a `sha256` the repo is not cached (the script would be frozen
at build time) and everything installs per launch. `--no-cache` disables caching.

# Base OS (`image:`)

The default is Ubuntu 26.04 LTS. Only `images:ubuntu/<release>` and
`ubuntu:<release>` (a number or codename) are accepted; anything else, including
other distros, remotes and local images, makes the file invalid and aborts the
launch, so a repo cannot point the host at an image it did not choose.
Precedence: `--image` (the host user may choose anything), then the repo's
`image:`, then the default; `--no-deps` ignores the repo's `image:`. Each image
gets its own cached base and deps image, so the first launch on a new release
builds once, and the session is told when `--image` overrode the file.

# Per-launch `setup:` scripts

Things that need the checkout or the user's home (a venv in the repo, a per-user
toolchain in `~/.local/bin`) cannot be baked into an image. Scripts under
`setup:` (`name`, `path`, optional `args`, `env`, `timeout` of 1-3600 seconds)
run at the start of **every** launch, as `timeout <N> bash -l ./<path> <args>`
(argv, no shell string), as `ubuntu` in the clone, after the clone and **before
Claude's credentials are copied in** (the repo's deploy key and approved USB
devices are present), with no stdin. A failure or timeout does not abort the
launch; it is logged and listed in the session's CLAUDE.md so the session can
tell you. `setup:` is not part of the cache key, so scripts should be idempotent
and fast when already done.

# Placed files (`files:`)

Pinned files copied under the session user's home at the start of every launch
(after the clone, before Claude's credentials and `setup:`). Sources are `url`
(fetched and checked on the host, like `binaries`) or `host_path` (read from
your disk); both need a `sha256`, which must match before the container sees a
byte. A `host_path` entry is a request: it is placed only with `--allow-files`
or a yes at the prompt (no terminal and no flag means skipped), and `--no-files`
ignores every entry. Destinations are `~/...` only and exclude shell startup,
ssh/gpg/git/Claude config and autostart locations, so a file cannot trivially
become a persistence hook. `on_error: skip` lets a failed entry (download, missing
file, hash mismatch, write error) pass with a note in the session's CLAUDE.md
instead of aborting the launch. Files are written as the session user, never
cached in an image. Field details: [Editing the deps file](../../session/common/deps-file.md).

# Hardening and remaining trust

- The strict schema rejects unknown keys, odd names, `-`-prefixed apt specs and
  loader/shell env vars such as `LD_PRELOAD` and `PATH`; any error aborts the
  launch.
- The host downloads everything (https only, including redirects; no credentials
  in URLs; size caps; the GitHub token is never sent) and checks sha256 *before*
  the bytes enter the container. Binaries must have a checksum. Archive members
  are read in memory, never unpacked.
- Install runs *before* the SSH deploy key and Claude credentials exist in the
  container, so an installer cannot read them, and uses argv lists, never a
  shell string.
- Remaining trust: apt repos, and any script without a `sha256` (it runs whatever
  its URL serves). The file and `setup:` scripts run automatically (as root, or
  with the deploy key) in future sandboxes, so review changes to them like code;
  the launcher holds any branch that changes them
  (see [Concurrent sessions](../guides/concurrent-sessions.md)).

[^deps]: Parse, validate and download the deps file
