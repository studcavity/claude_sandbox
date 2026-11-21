"""container/images.py - cached base/deps images and applying repo deps."""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import uuid

from claude_sandbox.container import deps as container_deps
from claude_sandbox.container import files
from claude_sandbox.container.incus import Container
from claude_sandbox.util import log, run

# Cached base image. The alias embeds a hash of the source image and the build
# steps below, so editing either one triggers a rebuild automatically.
IMAGE_PREFIX = "claude-sandbox-"
# Per-repo images with the deps baked in. The prefix must not start with
# IMAGE_PREFIX: base-image cleanup deletes every other image under its prefix.
DEPS_IMAGE_PREFIX = "claude-sandboxdeps-"
MAX_DEPS_IMAGES = 4  # keep the newest N; older ones are deleted (each is GBs)
# Base images, one per source image (repos may pick different Ubuntu releases):
# keep the newest N so alternating repos don't rebuild each other's base.
MAX_BASE_IMAGES = 4
BASE_APT_CLEAN = "apt-get clean && rm -rf /var/lib/apt/lists/*"
LOCK_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "claude_sandbox")
BASE_PACKAGES = (
    # session essentials
    "git", "curl", "openssh-client", "tmux", "ca-certificates",
    # python
    "python3-pip", "python3-venv", "python3-dev",
    # building native deps
    "build-essential", "pkg-config",
    # search / inspection tools Claude Code reaches for
    "ripgrep", "fd-find", "jq", "tree", "file", "less", "wget",
    "unzip", "zip", "xz-utils",
    # process / system debugging
    "procps", "psmisc", "lsof", "strace", "gdb", "htop",
    # bind9-dnsutils (provides dig) exists on 24.04 and 26.04; the older
    # `dnsutils` wrapper package is gone from 26.04.
    "iproute2", "bind9-dnsutils", "netcat-openbsd",
)
BASE_ROOT_SETUP = ("export DEBIAN_FRONTEND=noninteractive && "
                   "apt-get update -qq && "
                   "apt-get install -y -qq --no-install-recommends "
                   + " ".join(BASE_PACKAGES) + " && "
                   # Debian/Ubuntu ship fd as fdfind
                   "ln -sf /usr/bin/fdfind /usr/local/bin/fd && "
                   # a chatty service must not eat the root disk
                   "mkdir -p /etc/systemd/journald.conf.d && "
                   "printf '[Journal]\\nSystemMaxUse=100M\\nRuntimeMaxUse=50M\\n' "
                   "> /etc/systemd/journald.conf.d/size.conf && "
                   + BASE_APT_CLEAN)
CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"
# Reset per-instance identity so every container launched from the image
# gets its own.
BASE_CLEANUP = ("(command -v cloud-init >/dev/null && cloud-init clean --logs);"
                " truncate -s 0 /etc/machine-id; true")


def base_alias(source):
    h = hashlib.sha256("\0".join(
        (source, BASE_ROOT_SETUP, CLAUDE_INSTALL, BASE_CLEANUP)).encode())
    return IMAGE_PREFIX + h.hexdigest()[:12]


def deps_alias(base, key):
    return DEPS_IMAGE_PREFIX + hashlib.sha256(
        (base + "\0" + key).encode()).hexdigest()[:12]


def parse_image_list(out, prefix):
    """`<cli> image list --format json` output -> {alias: created_at (UTC)}."""
    found = {}
    for img in json.loads(out or "[]"):
        # e.g. 2026-11-14T23:15:00.123456789Z; drop the fraction to stay
        # portable across Python versions.
        created = datetime.datetime.strptime(
            img["created_at"][:19], "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=datetime.timezone.utc)
        for a in img.get("aliases") or []:
            if a["name"].startswith(prefix):
                found[a["name"]] = created
    return found


def cached_images(cli, prefix=IMAGE_PREFIX):
    """{alias: created_at (aware datetime)} for our local images."""
    out = run([cli, "image", "list", "local:", "--format", "json"],
              capture=True).stdout
    return parse_image_list(out, prefix)


def is_fresh(created, max_age_days, not_older_than=None, now=None):
    """True if an image built at `created` can be reused.

    It must exist, be younger than max_age_days, and (if given) be no older
    than `not_older_than` (e.g. the base image it was derived from).
    """
    if created is None:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now - created >= datetime.timedelta(days=max_age_days):
        return False
    return not_older_than is None or created >= not_older_than


def classify(created, max_age_days, hard_max_age_days=None, not_older_than=None,
             now=None):
    """'missing' | 'fresh' | 'stale' | 'expired' for an image built at `created`.

    fresh    younger than max_age_days and not older than `not_older_than`;
    stale    past max_age_days, or older than `not_older_than` (e.g. the base it
             derives from), but younger than the hard cap: usable now, worth
             refreshing in the background;
    expired  at or past the hard cap (default: max_age_days, i.e. nothing is
             ever merely stale and every caller rebuilds, as before).
    """
    if created is None:
        return "missing"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    hard = max(hard_max_age_days or 0, max_age_days)
    age = now - created
    if age >= datetime.timedelta(days=hard):
        return "expired"
    if (age >= datetime.timedelta(days=max_age_days)
            or (not_older_than is not None and created < not_older_than)):
        return "stale"
    return "fresh"


def images_to_prune(images, keep, current):
    """Oldest aliases to delete so at most `keep` remain; never `current`."""
    old = sorted((a for a in images if a != current), key=images.get)
    return old[:max(0, len(images) - keep)]


def _age(created):
    age = datetime.datetime.now(datetime.timezone.utc) - created
    return f"{age.days}d {age.seconds // 3600}h"


def build_image(cli, source, alias, prefix, provision, replace=False,
                root_size=None):
    """Launch `source`, run provision(container), publish it as `alias`.

    The build container is always deleted, even on failure. With replace,
    an existing image under `alias` is deleted first (running containers
    are unaffected; it only changes future launches).
    """
    build = Container(cli, f"{prefix}build-{uuid.uuid4().hex[:8]}")
    try:
        build.launch(source, ephemeral=False, root_size=root_size)
        build.wait_network()
        provision(build)
        build.root(BASE_CLEANUP, check=False, capture=True)
        run([cli, "stop", build.name], capture=True)
        if replace:
            run([cli, "image", "delete", alias], check=False, capture=True)
        log(f"publishing image {alias}")
        run([cli, "publish", build.name, "--alias", alias], capture=True)
    finally:
        build.delete()


def delete_images(cli, aliases, what="image"):
    for stale in aliases:
        run([cli, "image", "delete", stale], check=False, capture=True)
        log(f"removed stale {what} {stale}")


@contextlib.contextmanager
def build_lock(alias):
    """Exclusive host-wide lock for building `alias`, so concurrent launches
    build it once; the rest wait, then find the finished image."""
    os.makedirs(LOCK_DIR, mode=0o700, exist_ok=True)
    with open(os.path.join(LOCK_DIR, alias + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def ensure_base_image(cli, source, max_age_days, rebuild, root_size=None, *,
                      hard_max_age_days=None, on_stale=None, refresh=False):
    """Alias of the base image for `source`, building it if needed.

    With `hard_max_age_days` and `on_stale`, an image past max_age_days but
    inside the hard cap is used as is and `on_stale("base", alias)` is called so
    the caller can refresh it in the background; nobody waits for a rebuild
    that isn't strictly needed. `refresh=True` is the background refresher
    itself: it rebuilds a stale image (re-checking under the lock, so two
    refreshers never build twice).
    """
    alias = base_alias(source)
    if not rebuild and not refresh:   # decide WITHOUT the lock: a refresher may hold it for minutes
        created = cached_images(cli).get(alias)
        state = classify(created, max_age_days, hard_max_age_days)
        if state == "fresh" or (state == "stale" and on_stale):
            if state == "stale":
                log(f"using cached image {alias} (built {_age(created)} ago, past "
                    f"{max_age_days}d: refreshing it in the background)")
                on_stale("base", alias)
            else:
                log(f"using cached image {alias} (built {_age(created)} ago)")
            return alias
    with build_lock(alias):
        return _ensure_base_image(cli, source, alias, max_age_days, rebuild,
                                  root_size)


def _ensure_base_image(cli, source, alias, max_age_days, rebuild,
                       root_size=None):
    """Under the lock only a FRESH image is reusable (a stale one reaches here
    only when the caller can't use it, or is the refresher): re-checking here is
    what stops two refreshers, or a launch racing a refresher, building twice."""
    images = cached_images(cli)
    created = images.get(alias)

    if not rebuild and is_fresh(created, max_age_days):
        log(f"using cached image {alias} (built {_age(created)} ago)")
        return alias
    if created and not rebuild:
        log(f"cached image {alias} is older than {max_age_days}d, rebuilding")

    def provision(build):
        log("installing packages")
        build.root(BASE_ROOT_SETUP, capture=True)
        log("installing Claude Code")
        build.user(CLAUDE_INSTALL, capture=True)

    log(f"building base image {alias} from {source} (one-time)")
    build_image(cli, source, alias, IMAGE_PREFIX, provision,
                replace=alias in images, root_size=root_size)
    # Older recipes pile up; bound them, but keep other sources' recent bases.
    now = datetime.datetime.now(datetime.timezone.utc)
    delete_images(cli, images_to_prune({**images, alias: now},
                                       MAX_BASE_IMAGES, alias), "base image")
    return alias


APT_PREFIX = "claude-sandbox-"   # our files in apt's dirs; never a system name


def apt_source_text(r):
    """deb822 .sources text for one validated `apt_repos:` entry. Pure. The
    values were checked against plain-token patterns, so none can add a field."""
    lines = ["Types: deb", f"URIs: {r['url']}", f"Suites: {r['suite']}",
             "Components: " + " ".join(r["components"])]
    if r["architectures"]:
        lines.append("Architectures: " + " ".join(r["architectures"]))
    lines.append(f"Signed-By: {apt_key_path(r)}")
    return "\n".join(lines) + "\n"


def apt_key_path(r):
    ext = "asc" if r["key_url"].endswith(".asc") else "gpg"
    return f"/etc/apt/keyrings/{APT_PREFIX}{r['name']}.{ext}"


def add_apt_repos(box, repos):
    """Write each repo's pinned signing key and .sources file. The key is
    downloaded and sha256-checked on the host; the repo is trusted only for
    packages signed by that key (Signed-By), never added to the global keyring."""
    box.root_argv(["install", "-d", "-m", "0755", "/etc/apt/keyrings"])
    for r in repos:
        log(f"adding apt repo {r['name']}")
        key = container_deps.fetch(r["key_url"], container_deps.MAX_KEY,
                                   r["key_sha256"])
        box.put_file(apt_key_path(r), key, "0644")
        box.put_file(f"/etc/apt/sources.list.d/{APT_PREFIX}{r['name']}.sources",
                     apt_source_text(r).encode(), "0644")


def apply_deps(box, deps, verify=None):
    """Install parsed, validated deps into a container (apt repos, apt, binaries, files, scripts).

    Everything is downloaded and checksum-verified here on the host; the
    container only receives bytes. Must run before any credential is in the
    container (the per-launch container and the image build container both
    satisfy that).

    `verify(data, repo)` checks a build attestation for a binary that asks for
    one (`attest:`); it raises on failure. A binary that asks for one when no
    verifier is given is refused: attestation is never silently skipped.
    """
    repos = deps.get("apt_repos") or []
    if repos:
        add_apt_repos(box, repos)
    if deps["apt"] or repos:
        log("installing apt packages" if deps["apt"] else "updating apt")
        env = {"DEBIAN_FRONTEND": "noninteractive"}
        # always after the repos are added, so their packages are visible
        box.root_argv(["apt-get", "update", "-qq"], env=env)
    if deps["apt"]:
        box.root_argv(["apt-get", "install", "-y", "-qq",
                       "--no-install-recommends", *deps["apt"]], env=env)

    for b in deps["binaries"]:
        log(f"installing binary {b['name']}")
        data = container_deps.fetch(b["url"], container_deps.MAX_BINARY, b["sha256"])
        if b.get("attest"):
            if verify is None:
                raise container_deps.DepsError(
                    f"binary {b['name']} needs an attestation check "
                    "but no verifier was given")
            log(f"verifying the {b['attest']} attestation of {b['name']}")
            verify(data, b["attest"])
        if b["archive_path"]:
            data = container_deps.extract_member(data, b["archive_path"])
        box.put_file(f"/usr/local/bin/{b['name']}", data, "0755")

    for f in deps.get("files") or []:
        # Pinned files (downloads and approved host files that must not fail
        # open): written as the session user, after the installs above.
        log(f"placing file {f['dest']}")
        box.put_user_file(f["dest"][2:], files.load(f), f["mode"])

    if deps["scripts"]:
        scratch = "/var/tmp/sandbox-deps"
        box.root_argv(["install", "-d", "-m", "0700", scratch])
        try:
            for s in deps["scripts"]:
                log(f"running install script {s['name']}"
                    + ("" if s["sha256"] else " (no sha256 pinned!)"))
                data = container_deps.fetch(s["url"], container_deps.MAX_SCRIPT,
                                          s["sha256"])
                path = f"{scratch}/{s['name']}.sh"
                box.put_file(path, data, "0700")
                box.root_argv(["bash", path, *s["args"]], cwd=scratch,
                              env=s["env"])
        finally:
            box.root_argv(["rm", "-rf", scratch], capture=True)


def ensure_deps_image(cli, base, deps, max_age_days, rebuild,
                      root_size=None, *, hard_max_age_days=None, on_stale=None,
                      refresh=False, verify=None):
    """Alias of a per-deps image derived from `base`, building it if needed;
    None when these deps must be installed per launch instead.

    The image holds exactly what apply_deps() installs, built in a throwaway
    container that has no credentials, from host-verified downloads. It is
    keyed by the base image and the validated install sections, rebuilt when
    older than max_age_days (unpinned apt packages go stale) or older than the
    base image (so a refreshed Claude Code isn't shadowed), or on --rebuild.

    As for the base image: with `hard_max_age_days` and `on_stale`, a stale one
    (including one merely older than a freshly refreshed base) is used now and
    refreshed in the background; `refresh=True` is that background refresher.
    """
    key = container_deps.cache_key(deps)
    if key is None:
        if any(s["sha256"] is None for s in deps["scripts"]):
            log("deps image cache off: an install script has no sha256 pin")
        return None
    alias = deps_alias(base, key)

    if not rebuild and not refresh:   # decide without the lock (see ensure_base_image)
        created = cached_images(cli, DEPS_IMAGE_PREFIX).get(alias)
        state = classify(created, max_age_days, hard_max_age_days,
                         cached_images(cli).get(base))
        if state == "fresh" or (state == "stale" and on_stale):
            if state == "stale":
                log(f"using cached deps image {alias} (built {_age(created)} ago, "
                    "older than allowed: refreshing it in the background)")
                on_stale("deps", alias)
            else:
                log(f"using cached deps image {alias} (built {_age(created)} ago)")
            return alias

    with build_lock(alias):
        created = cached_images(cli, DEPS_IMAGE_PREFIX).get(alias)
        base_created = cached_images(cli).get(base)
        if not rebuild and is_fresh(created, max_age_days, base_created):
            log(f"using cached deps image {alias} (built {_age(created)} ago)")
            return alias

        def provision(build):
            apply_deps(build, deps, verify)
            build.root(BASE_APT_CLEAN, capture=True)

        log(f"building deps image {alias} (one-time per change)")
        build_image(cli, base, alias, DEPS_IMAGE_PREFIX, provision,
                    replace=created is not None, root_size=root_size)

        # Bound disk use: keep the newest MAX_DEPS_IMAGES (never this one).
        images = cached_images(cli, DEPS_IMAGE_PREFIX)
        delete_images(cli, images_to_prune(images, MAX_DEPS_IMAGES, alias),
                      "deps image")
    return alias
