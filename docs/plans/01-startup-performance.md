# 1. Startup performance and `--timings`

## Baseline (measured 2026-11-15, from inside a launcher-built sandbox)
No real end-to-end launch was possible there (no nested incus, no gh login),
so this is exact call counts from the real `main()` against test doubles plus
measured timings of each primitive. **Container boot, volumes and the real
image-copy cost were not measured**; `--timings` (1a) exists to close that gap.

Counts before the session starts: ~11 serial `gh` process spawns, ~17
container execs; `GET /repos/{repo}` is fetched at least twice.

| Measured | Time |
|---|---|
| one `gh api` call (spawn + TLS + RTT to api.github.com) | ~0.35 s |
| `git clone` Vape-Hacks (13 MiB pack) | 3.2 s (`--depth=1`: 2.8 s; blobless: 4.2 s) |
| ssh handshake to github.com | 0.54 s |
| Claude start to input prompt (seeded, warm) | 0.6 s |
| `bash -lc`, python3, git config, ssh-keygen (each) | 1-10 ms |
| **cold**: Claude binary download (366 MB) | 49 s |
| **cold**: Ubuntu image (~135 MiB compressed) | ~35 s at 4 MB/s |

Warm launch, estimated: gh calls ~4-5 s, update-check fetch ~0.5 s, clone ~3 s,
execs ~1 s (estimate), boot/volumes/network wait unknown.

## 1a. `--timings` flag (do first)
Goal: real per-stage numbers on the user's host.

- New stdlib-only module `sandbox_timing.py`: `Timings` with
  `stage(name)` context manager (monotonic clock, nests via a `parent` field),
  `counter(name)` for gh calls and execs, `report()` returning rows
  `(stage, seconds, share)`, and `to_json()`.
- Instrument by wrapping, not editing, the calls in `main()`:
  login, token_scopes, pool check, load_deps, resolve_usb, preflight_github,
  ensure_base_image, ensure_deps_image, launch, wait_network, self_check,
  attach_volumes, install_deps, provision_key, install_claude, clone,
  run_setup, write_claude_md, seed_claude, update check, land, teardown.
  Inside `Container`/`gh_api`, bump counters (calls, seconds) rather than
  adding stages, so the report also shows "N gh calls, X s total".
- Flags: `--timings` prints a table to stderr at exit (also on error);
  `--timings-json PATH` writes it for comparison across runs.
- Overhead must be ~0 when off: `Timings` is a no-op object by default.
- Tests: fake clock; nested stages; report still produced when a stage raises;
  no-op mode allocates nothing; JSON round-trips.
- Note the time to the *session prompt* is the user's real metric: record
  "launch -> `claude` process exec" as a headline row.
- Size: small. Conflict risk with other sessions: high in `main()`; keep the
  diff to one-line `with t.stage(...)` wraps and rebase just before pushing.

## 1b. Concurrency (needs 2 for the best result)
- Run independent GitHub reads together: login, token scopes, deps file, host
  file, protection check, stale-key reap, `/meta`, repo owner. Today ~4-5 s;
  expected ~0.5-1 s. With the `gh` CLI use a thread pool of subprocesses
  (works now); with a typed client (plan 2) one pooled session removes
  repeated process spawn and TLS handshakes too.
- Fetch `GET /repos/{repo}` **once** and share it between
  `sandbox_sessions.check_protection` and the owner check added for
  pre-trusting (that second fetch was introduced by commit 635516d).
- Order constraint to keep: deps file must be read before the image is chosen;
  USB approval before image build (existing comments explain why).
- Advisory work (protection advice, key reaping, update check) never blocks a
  launch, so run it in threads overlapping image resolve + container boot.
  Join before the session starts so warnings still print, and so the deploy
  key reaping finishes before a new key is created.
- Tests: ordering guarantees (deps before image, join before session),
  exceptions in a worker don't abort the launch for advisory tasks and do for
  required ones.

## 1c. Stale-while-revalidate image cache (largest worst-case win)
Today `max_age` (7 days) forces a synchronous rebuild: ~90 s of downloads plus
apt, once a week and on every deps change.
- If a cached image exists but is older than `max_age`, launch from it now and
  start a **detached** rebuild for next time (`build_lock` already serialises
  builders). Hard-expire at a larger cap (e.g. 30 days) and rebuild
  synchronously beyond it.
- Trade-off to state in the log line: a stale image carries an older Claude
  Code and older packages.
- Never apply to a changed deps hash: that is a different alias and must be
  built before launch.
- Needs a real-container run (`sandbox_images.py`).

## 1d. Batch container execs
~17 execs, each a separate `incus exec` + `bash -lc`. Fold:
self_check + marker + package probe; ssh-keygen + pubkey read + known_hosts;
the four `git config` calls. Use `Container.root_argv` / one quoted script,
never string-built shell (see CLAUDE.md). Expected 0.5-0.8 s (unmeasured; 1a
will tell). Also poll `wait_network` every 0.2 s instead of 1 s.

## 1e. Check the storage driver (possibly the biggest warm cost)
On a `dir` pool every ephemeral launch **copies** the whole image rootfs
(including the 366 MB Claude binary); `zfs`/`btrfs` clone copy-on-write.
- Add to `sandbox_pool.check`: read the pool driver and, if `dir`, print one
  line suggesting a CoW pool and how to create one. Never change the user's
  pool.
- 1a's `launch` stage will confirm whether this matters.

## Not worth doing
Shallow/blobless clones (measured: no gain; the working tree dominates).
Execs are individually cheap (1-10 ms of in-container work).

## Suggested order
1a -> collect real numbers -> 1e/1b/1d as the numbers justify -> 1c.
