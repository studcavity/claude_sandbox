"""cli.py - the launcher's entry: `main()` and the launch sequence.

Options and defaults are in args.py; the per-container lifecycle is sandbox.py."""
import os
import subprocess
import signal
import sys
from claude_sandbox import paths
from claude_sandbox import timing
from claude_sandbox import update
from claude_sandbox.backends import github as backends_github
from claude_sandbox.backends import local as backends_local
from claude_sandbox.container import pool
from claude_sandbox.github import gh
from claude_sandbox.container.incus import Container
from claude_sandbox.github.gh import GitHubError
from claude_sandbox.container.images import ensure_base_image, ensure_deps_image
from claude_sandbox.util import log
from claude_sandbox import options
from claude_sandbox import sandbox
from claude_sandbox import skills


def refresh_argv(args, python, script):
    """argv of the detached process that refreshes stale cached images: the
    same image-relevant options, so it works out the same aliases."""
    cmd = [python, script, "--refresh-images", "--cli", args.cli,
           "--max-age", str(args.max_age),
           "--hard-max-age", str(args.hard_max_age)]
    if args.repo:
        cmd += ["--repo", args.repo]
    if getattr(args, "local", None):
        cmd += ["--local", args.local]
        if args.base:
            cmd += ["--base", args.base]
    if args.image:
        cmd += ["--image", args.image]
    if args.disk_size:
        cmd += ["--disk-size", str(args.disk_size)]
    if args.no_deps:
        cmd.append("--no-deps")
    return cmd


def recovery_image(args):
    """The image for --recover's helper container: the cached base image
    (it has git), built if needed."""
    return ensure_base_image(args.cli, args.image or options.DEFAULT_IMAGE,
                             args.max_age, False, args.disk_size,
                             hard_max_age_days=args.hard_max_age)


def refresh_log_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "claude_sandbox", "image-refresh.log")


def spawn_refresh(args):
    """Start the refresher detached (its own session, output to a log) so the
    launcher neither waits for it nor is held open by it."""
    path = refresh_log_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "a") as logf:
            subprocess.Popen(
                refresh_argv(args, sys.executable, paths.MAIN_SCRIPT),
                stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                start_new_session=True)
    except OSError as e:
        log(f"warning: couldn't start the background image refresh: {e}")
        return
    log(f"refreshing stale cached images in the background (log: {path})")


def refresh_images(args):
    """Rebuild whatever cached image is stale for this repo's settings, then
    exit. Safe to run twice at once: each rebuild re-checks under the lock."""
    box = sandbox.Sandbox(args)
    try:
        box.load_deps()
        source = box.choose_image()
        image = ensure_base_image(args.cli, source, args.max_age, False,
                                  args.disk_size, hard_max_age_days=args.hard_max_age,
                                  refresh=True)
        if box.deps:
            ensure_deps_image(args.cli, image, box.deps, args.max_age, False,
                              args.disk_size, hard_max_age_days=args.hard_max_age,
                              refresh=True)
    except (GitHubError, subprocess.CalledProcessError, RuntimeError, OSError) as e:
        log(f"error: image refresh failed: {e}")
        return 1
    log("image refresh done")
    return 0


def main(argv=None):
    p = options.build_parser()
    args = p.parse_args(argv)
    try:
        options.normalize_args(args)
    except ValueError as e:
        p.error(str(e))
    if not os.path.isfile(args.claude_md):
        p.error(f"--claude-md: no such file: {args.claude_md}")

    timings = None
    if args.timings or args.timings_json:
        timings = timing.Timings()
        timing.install(timings)
    try:
        return _launch(args)
    finally:
        gh.clear_prefetch()
        if timings is not None:
            timing.install(None)
            print("\n" + timings.report(), file=sys.stderr)
            if args.timings_json:
                try:
                    timing.write_json(timings, args.timings_json)
                    log(f"timings written to {args.timings_json}")
                except OSError as e:
                    log(f"warning: couldn't write {args.timings_json}: {e}")


def _skills_ready():
    """Update the skill submodules; False (after saying why) only when that
    failed and no skill is checked out to fall back on."""
    ok, detail = skills.refresh()
    verdict = skills.decide(ok, bool(skills.find_skills()))
    if verdict == "missing":
        print("error: couldn't fetch the Claude skills and none are checked out "
              f"({detail}). Run: git submodule update --init", file=sys.stderr)
        return False
    if verdict == "stale":
        log(f"warning: couldn't update the Claude skills ({detail}); "
            "using the copy already checked out")
    return True


def _launch(args):
    if args.refresh_images:
        return refresh_images(args)
    if not args.no_update_check:
        with timing.stage("update check (git fetch)"):
            update.check_for_updates(
                paths.ROOT, sys.argv,
                interactive=sys.stdin.isatty(), ask=input)

    if args.land_all:
        return backends_github.land_all(args)
    if args.recover:
        return backends_local.recover(args, lambda: recovery_image(args))

    if not args.setup_token:
        with timing.stage("skills (git submodule update)"):
            if not _skills_ready():
                return 1

    box = sandbox.Sandbox(args)
    gh.start_prefetch(box.backend.prefetch_plan())
    if not args.setup_token:
        with timing.stage("host checks (login, scopes)"):
            bad = box.backend.check_host()  # fail fast, before building anything
            if bad:
                print(f"error: {bad}", file=sys.stderr)
                return 1

    def on_signal(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGHUP, on_signal)

    with timing.stage("incus pool check"):
        pool_err = pool.check(args.cli, args.grow_pool)
    if pool_err:
        print(f"error: {pool_err}", file=sys.stderr)
        return 1

    try:
        if not args.setup_token:
            with timing.stage("usb requests + deps file"):
                box.resolve_usb()  # prompt now, not after a long image build
                box.resolve_mounts()
                box.resolve_nesting()  # before load_deps/launch read args.nesting
                box.load_deps()  # before the image: it may pick the base OS
                box.resolve_files()  # prompt now, not after a long image build
            with timing.stage("preflight (protection, stale keys)"):
                box.backend.preflight(lambda: Container.list_names(args.cli),
                                      box.name)
        source = box.choose_image()
        image = source
        stale: list = []
        swr = dict(hard_max_age_days=args.hard_max_age or None,
                   on_stale=lambda kind, alias: stale.append((kind, alias)))
        with timing.stage("images (cache lookup / build)"):
            if not args.no_cache:
                image = ensure_base_image(args.cli, source,
                                          args.max_age, args.rebuild,
                                          args.disk_size, **swr)
            if box.deps and not args.no_cache:
                baked = ensure_deps_image(args.cli, image, box.deps,
                                          args.max_age, args.rebuild,
                                          args.disk_size, **swr)
                if baked:
                    image, box.deps_baked = baked, True
                    box.deps_note += ("\n(Preinstalled in a cached image; edits "
                                      "to the file apply on the next launch.)")
        with timing.stage("container launch + boot"):
            box.launch(image)
        if stale and not args.setup_token:   # after the launch: never competes with it
            spawn_refresh(args)
        if args.setup_token:
            box.install_claude()
            return box.setup_token()
        with timing.stage("volumes"):
            box.attach_volumes()  # before the clone, so it lands on its own volume
        with timing.stage("install deps"):
            box.install_deps()  # before any credential is in the container
            box.setup_nesting()
            box.attach_usb()
        with timing.stage("bind mounts"):
            box.attach_mounts(box.backend.workdir())
        with timing.stage("ssh key + deploy key + git config"):
            box.provision_key()
        with timing.stage("claude install check"):
            box.install_claude()
        with timing.stage("clone"):
            workdir = box.clone()
        with timing.stage("repo files"):
            box.place_files(workdir)  # before setup (which may use them) and credentials
        with timing.stage("repo setup scripts"):
            box.run_setup(workdir)  # before Claude credentials reach the container
        with timing.stage("session tools + CLAUDE.md"):
            box.install_session_tools()
            box.install_skills()
            box.install_session_docs()
            box.write_claude_md(workdir)
        with timing.stage("session (interactive)"):
            box.session(workdir)
    except KeyboardInterrupt:
        log("interrupted")
    except (subprocess.CalledProcessError, RuntimeError) as e:
        log(f"error: {e}")
        err = getattr(e, "stderr", None)
        if err:
            print(err.decode(errors="replace") if isinstance(err, bytes)
                  else err, file=sys.stderr)
        return 1
    finally:
        # Teardown FIRST: revoke the key and destroy the container, so nothing
        # the session left running (a nohup'd loop, a tmux pane) can push
        # between the landing's checks and the PRs it opens. Landing needs
        # only the user's gh login, never the key or the container.
        try:
            with timing.stage("export work"):
                box.export()  # container still up; never raises
            with timing.stage("teardown"):
                box.teardown()
        finally:
            try:
                with timing.stage("land: PRs, provenance"):
                    box.land()
                box.report_host_warnings()
            finally:
                # Volume deletion runs beside landing; finish it before exit.
                with timing.stage("volumes: wait for delete"):
                    box.wait_volumes()
    return 0
