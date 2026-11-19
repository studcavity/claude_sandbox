---
type: Decision
title: "Spec 04: a narrower secret scan, aimed at what the sandbox actually holds"
description: "Narrow the secret scan to the credentials a sandbox actually holds."
tags: [decision, spec]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
---

# Spec 04: a narrower secret scan, aimed at what the sandbox actually holds

Source: `docs/dev/plans/simplification.md` item 4. Status: implemented, option A (option B, an exact-match tripwire on the host, not done; push protection unverified). It doesn't
depend on the other specs, though specs 01-03 remove some of the call sites
below.

## Problem

`sandbox_untrusted.find_secrets` has six token regexes, plus an
entropy-gated `name = value` rule (`ASSIGNMENT`, `MIN_ENTROPY`). It runs in
five places:

| Where | What it scans | Removed by |
|---|---|---|
| `sandbox_check.message_secret_findings` | commit messages | – |
| `sandbox_check.added_secret_findings` | every added line, plus the `secret-scan: allow` marker | – |
| `sandbox_land.safe_line` | PR title/body lines (subjects, record section) | partly by spec 06 |
| `sandbox_actions._issue` | issue title/body and the `todo/` brief | specs 02/03 change it |
| `sandbox_check.override_description` | the `--override` reason | spec 01 (option A) |

The entropy rule is the part that produces false positives, and it is why
the allow marker exists. The marker is in-band: the change being judged can
mark itself as allowed. The PR-body and issue scans re-scan text that is
already in commits the checker scanned.

## What the plan gets right, and what to check

The plan proposes GitHub **push protection**, which blocks a known
credential *before* it lands in history, plus optional gitleaks or
trufflehog in CI. Two claims need checking before anything is deleted (they
were flagged as unverified in `1e1d100` and are still unverified):

1. Is push protection available on the user's repos? My understanding is
   that it is free for public repos but needs a paid plan for private ones.
   Confirm this for the actual repos and plan, and don't rely on this note.
2. Does it apply to pushes made with a **deploy key**, and does it detect
   the credential types below?

## The threat model, made concrete

The sandbox holds exactly three credentials (see `claude_sandbox.py`:
`claude_secrets`, the `ssh-keygen` call in the key setup, and
`sandbox_provenance.container_script`):

- the Claude credential: `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`,
  both starting `sk-ant-`;
- the deploy key, an OpenSSH ed25519 private key at `~/.ssh/id_ed25519`;
- the session signing key, another OpenSSH ed25519 private key.

A session can only leak secrets it can see. The ones that matter most are
these three, plus anything a repo's own setup puts in the container. Two
existing patterns (`sk-ant-`, `BEGIN ... PRIVATE KEY`) already cover all
three. The GitHub/AWS/Slack patterns guard against credentials the sandbox
never holds.

## Options

- **A. Prefix patterns only.** Keep the cheap prefix patterns (at least
  `sk-ant-` and private-key headers; the GitHub token patterns are cheap
  too). Drop `ASSIGNMENT`/entropy and the allow marker. Rely on push
  protection where available. This is the smallest diff.
- **B. Exact-match tripwire on the host.** The launcher knows the exact
  values it put in the container: it holds the token, and it can fingerprint
  the generated keys. At landing, it could check the PR's commit messages
  and diff for those literal values, or for a distinctive substring of each.
  That gives no false positives and needs no allow marker. It misses encoded
  copies, as every scanner does. This could replace or back up A.
- **C. An off-the-shelf scanner in CI** (gitleaks or trufflehog) instead of
  our regexes, following the "build versus buy" order in `CLAUDE.md`. Weigh
  this against keeping the checker stdlib-only and fast: it would be a
  separate CI step, not part of `sandbox-check`.

I'd lean towards A, plus B if it turns out cheap, but whoever implements
this should decide.

## Done when

- One scan function with a short, justified pattern list. Each remaining
  pattern names the credential it is for.
- The scan runs once on the way in (commits) and, if anywhere else, only
  where the text didn't come from commits already scanned. Say in the commit
  why each remaining call site is needed.
- The allow marker is gone, or kept with a stated reason. Docs no longer
  tell sessions to assemble fixtures at runtime unless that is still needed.
- README says whether push protection is relied on, and how to turn it on.
- Commit says what is no longer caught (for example, generic
  `password = <high-entropy>` assignments) and why that's acceptable.

## Verify

- Unit tests: each kept pattern catches a fixture built at runtime
  (`"sk-ant-" + "x" * 30`), each removed rule no longer fires, and messages
  still report the kind of secret, never the value.
- If B: a test with a fake token and key passed in as "known values".
- Manually, on a repo with push protection: a push from a sandbox with a
  test credential is blocked. This needs a host and a real repo.
