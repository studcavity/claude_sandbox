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
it through the GitHub API, validates it against its schema, downloads
everything, and bakes the result into a cached image.[^deps] The session is
told how to edit it by `docs/session/common/deps-file.md`, shipped into its
container.

The format is the same page the session reads, so it lives in one place:
[Editing the deps file](../../session/common/deps-file.md).

Install order is apt, binaries, downloads, scripts. `--no-deps` skips all of it. Two
optional metadata keys, `last_generated` (quoted ISO 8601 UTC timestamp) and
`platform` (for example `lsb_release -a` plus `uname -m`), record where the
toolchain was resolved so it can be reproduced; they are validated but never
executed and do not affect caching. Scripts under `check:` are described in
[The final check](final-check.md).

# prek, installed everywhere

Every sandbox gets [prek](https://github.com/j178/prek) (pinned to v0.5.11 in
`container/deps.py`: per-CPU URL, sha256 and `attest: j178/prek`) added to the
binaries, even with no deps file or with `--no-deps`; a `prek` entry in the
repo's own `binaries:` wins. It is baked into the cached image like any binary.
If the clone has a `.pre-commit-config.yaml` at its root, the launcher also runs
puts prek's agent skill in `~/.claude/skills/prek/` after the clone: the host
downloads the one `SKILL.md` at the same tag, checks its pinned sha256 and
copies it in, so the container needs no `gh` (a failure is only a note). Also
`prek run --all-files` joins the [final check](final-check.md). To bump prek,
change the version, both digests and the skill's digest together.

**Host requirement:** the host's `gh` must be 2.90 or newer and logged in; the
launcher uses it to attest the prek download (`gh attestation verify`).

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

# Pinned files (`downloads:` and host `files:`)

Pinned files copied under the session user's home. `downloads:` (in this file) fetches
an https `url`; `files:` in `claude_sandbox_host.yaml` reads a `host_path` from your
disk. Both need a `sha256`, checked on the host before the container sees a byte, a
`~/...` destination (shell startup, ssh/gpg/git/Claude config and autostart locations are
refused), an optional quoted octal `mode` and `on_error: fail|skip`.

They are part of the image cache: entries are installed with the deps and baked in, keyed
by `dest`/`mode`/`sha256` (not by URL or host path), so a new pin builds a new image and an
unapproved host file can never be in one a launch reuses. An `on_error: skip` entry is
tried on the host first; if it can't be loaded and verified it stays out of the image and
its key, and is placed after the clone on every launch instead, so a failure is never
cached. A `downloads:` entry needs no approval (like `binaries`; `--no-deps` skips
it); a host `files:` entry is a request, placed only with `--allow-files` or a yes at the
prompt (no terminal and no flag means skipped), and `--no-files` ignores them. Files are
written as the session user. Field details:
[Editing the deps file](../../session/common/deps-file.md).

# Unknown keys

Unknown keys, in this file and the host file, are ignored with a warning (logged at
launch and listed in the session's CLAUDE.md) instead of aborting, so a repo can adopt a
newer format without breaking older launchers. Known keys are validated as strictly as
before and missing required keys still abort. A misspelt optional key is therefore ignored
and its default applies (for example `on_eror` leaves `on_error: fail`).

# Hardening and remaining trust

- The schema rejects odd names, `-`-prefixed apt specs and
  loader/shell env vars such as `LD_PRELOAD` and `PATH`; any error aborts the
  launch.
- The host downloads everything (https only, including redirects; no credentials
  in URLs; size caps; the GitHub token is never sent) and checks sha256 *before*
  the bytes enter the container. Binaries must have a checksum, and may carry
  `attest: owner/repo`: the host then runs `gh attestation verify` (host gh 2.90+,
  logged in) on the bytes and aborts the build if it fails; it is never skipped
  silently. Archive members are read in memory, never unpacked.
- Install runs *before* the SSH deploy key and Claude credentials exist in the
  container, so an installer cannot read them, and uses argv lists, never a
  shell string.
- Remaining trust: apt repos, and any script without a `sha256` (it runs whatever
  its URL serves). The file and `setup:` scripts run automatically (as root, or
  with the deploy key) in future sandboxes, so review changes to them like code;
  the launcher holds any branch that changes them
  (see [Concurrent sessions](../guides/concurrent-sessions.md)).

[^deps]: Parse, validate and download the deps file
