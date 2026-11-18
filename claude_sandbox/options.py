"""
claude_sandbox/cli.py - throwaway LXD/Incus container for a Claude Code session.

What it does:
  1. Launches an ephemeral container.
  2. Generates an SSH key *inside* the container. The private key never
     touches the host.
  3. Registers the public key with GitHub, either as a deploy key on one
     repo (recommended) or, only with --account-key, as an account key.
  4. Installs Claude Code and drops you into it (or a shell with --shell).
     Packages and Claude Code are baked into a cached local image
     (claude-sandbox-<hash>) on first run, so later launches skip the
     install. The image is rebuilt when the recipe or --image changes, when
     it is older than --max-age days, or on --rebuild. --no-cache disables it.
     A repo's claude_sandbox_deps.yaml is baked into a second per-repo image
     (claude-sandboxdeps-<hash>) the same way, unless it has an unpinned
     install script; see README.md.
  5. On exit (normal, error, Ctrl-C, SIGTERM, SIGHUP) deletes the GitHub key
     and destroys the container.

GitHub auth: all GitHub calls go through the host's `gh` CLI (run locally with
invoke), so log in with `gh auth login` (or set GH_TOKEN / GITHUB_TOKEN for gh).
No token is handled by this program. The launcher refuses to run when gh is
logged in as anyone but --github-user (override: --no-user-check).
  - Keys inherit the token's scopes when pushing: without `workflow` the
    sandbox can't push .github/workflows/ changes. Keep it that way: a pushed
    workflow runs with the repo's Actions secrets before any review. The
    launcher warns at startup when the token has it.
  - Account key mode needs the `admin:public_key` scope
    (gh auth refresh -s admin:public_key).
  - Deploy key mode needs admin rights on the repo
    (fine-grained token: "Administration: read & write").

Claude auth, first match wins:
  - CLAUDE_CODE_OAUTH_TOKEN on the host, or the token file
    (~/.config/claude_sandbox/oauth_token; create it with --setup-token,
    which runs `claude setup-token` in a throwaway sandbox). Long-lived and
    read-only, so it is safe to share between concurrent sessions.
  - ANTHROPIC_API_KEY on the host (API billing).
  - Otherwise, run /login inside Claude Code.
Secrets are piped into the container over stdin, never put on a command line.

Before launching, it checks whether this checkout is behind its upstream and,
on a terminal, offers to `git pull` (skip with --no-update-check). On first run
it creates ./venv from requirements.txt and re-runs itself inside it.

Examples:
  ./run.sh myproject                    # deploy key + clone of <github-user>/myproject
  ./run.sh other-user/proj              # a repo under another owner
  ./run.sh proj --model opus            # pick the session model
  ./claude_sandbox.py --local ~/src/proj           # a git repo on this machine, no GitHub
  ./claude_sandbox.py --account-key                # no repo: account-wide key
  ./claude_sandbox.py --setup-token                # one-time Claude login token
"""

import argparse
import os
import re
from claude_sandbox import paths
from claude_sandbox import provenance
from claude_sandbox import sessions
from claude_sandbox.container import deps
from claude_sandbox.container import host
from claude_sandbox.container import mounts
from claude_sandbox.backends.local import resolve_local
from claude_sandbox.github.gh import resolve_repo, validate_repo


# Defaults for the maintainer's setup; each can be overridden by a flag or the
# environment variable noted in --help.
DEFAULT_GITHUB_USER = "studcavity"


DEFAULT_GIT_EMAIL = "353176155+studcavity@users.noreply.github.com"


DEFAULT_IMAGE = "images:ubuntu/26.04"  # Ubuntu 26.04 LTS


# Storage quotas (incus sizes). The root disk was 10GiB by default, which a
# deps install or a nested incus overran. Quotas are caps, not allocations, so
# these cost nothing until used, and they bound what one session can take from
# the host's pool. 0 disables.
DEFAULT_DISK_SIZE = "30GiB"


DEFAULT_VOLUME_SIZE = "20GiB"


DEFAULT_CLI = "incus"


DEFAULT_TOKEN_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "claude_sandbox", "oauth_token")


DEFAULT_USB_ALLOW = os.path.join(os.path.dirname(DEFAULT_TOKEN_FILE),
                                 "usb_allow")


DEFAULT_MODEL = "claude-sonnet-5-5"


def size_arg(text):
    """argparse type for sizes: an incus size like 30GiB, or 0 to disable.
    Returns None for 0 so callers can treat it as "no quota"."""
    if text.strip() == "0":
        return None
    if not re.fullmatch(r"[1-9][0-9]*(\.[0-9]+)?(B|[kMGTPE]i?B?)", text.strip()):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a size like 30GiB (or 0 for no limit)")
    return text.strip()


# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(prog="claude_sandbox", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("repository", nargs="?", metavar="REPOSITORY",
                   help="repo to clone with a repo-scoped deploy key: a bare "
                        "name is looked up under --github-user, owner/name "
                        "is used as given")
    p.add_argument("--repo", metavar="OWNER/NAME",
                   help="same as REPOSITORY but requires the full owner/name")
    p.add_argument("--local", metavar="PATH",
                   help="work on a git repository on this machine instead of "
                        "GitHub: its --base branch is copied in as a git "
                        "bundle, and the session's sbx/<session>/* branches "
                        "are imported back into it when the session ends "
                        "(no network credential; see "
                        "docs/specs/08-local-backend.md)")
    p.add_argument("--base", metavar="BRANCH",
                   help="with --local: the branch to start from (default: "
                        "the repository's checked-out branch)")
    p.add_argument("--recover", action="store_true",
                   help="with --local: import the work that crashed launches "
                        "left on kept volumes (each is mounted read-only in a "
                        "throwaway container to export it), then exit")
    p.add_argument("--account-key", action="store_true",
                   help="no repo: register the key on your whole GitHub "
                        "account instead (needs admin:public_key; the "
                        "sandbox can then push to any repo you can). "
                        "Required to run without a repository")
    p.add_argument("--github-user", metavar="USER",
                   default=os.environ.get("CLAUDE_SANDBOX_GITHUB_USER")
                   or DEFAULT_GITHUB_USER,
                   help="GitHub account for bare repo names, and the only "
                        "account `gh` may be logged in as (default: "
                        f"$CLAUDE_SANDBOX_GITHUB_USER or {DEFAULT_GITHUB_USER})")
    p.add_argument("--no-user-check", action="store_true",
                   help="don't require gh to be logged in as --github-user")
    p.add_argument("--no-update-check", action="store_true",
                   help="don't check whether this checkout is behind its "
                        "upstream (and offer to git pull)")
    p.add_argument("--read-only", action="store_true",
                   help="make the deploy key read-only (no push)")
    p.add_argument("--no-pr", action="store_true",
                   help="don't open PRs for the session's branches when it "
                        "ends (they stay on GitHub under sbx/<session>/)")
    p.add_argument("--no-prefetch", action="store_true",
                   help="make the launch's GitHub reads one at a time instead "
                        "of starting the independent ones together")
    p.add_argument("--timings", action="store_true",
                   help="print where the launch spent its time (stages, and "
                        "how many gh / incus calls ran) when it ends")
    p.add_argument("--timings-json", metavar="PATH",
                   help="also write the timings as JSON (implies --timings), "
                        "to compare runs")
    p.add_argument("--no-provenance", action="store_true",
                   help=f"don't post the `{provenance.CONTEXT}` commit "
                        "status (every commit signed by the session's own key "
                        "or yours) on the session's PRs")
    p.add_argument("--no-hardening-prompt", action="store_true",
                   help="don't offer to tighten the repo's Actions settings "
                        "(read-only default token, Actions can't approve PRs); "
                        "the check and its warnings still run")
    p.add_argument("--no-protect-prompt", action="store_true",
                   help="don't offer to apply the default-branch protection "
                        "ruleset when the repo lacks one (the warning still "
                        "prints)")
    p.add_argument("--land-all", action="store_true",
                   help="open PRs for every session's unmerged sbx/* branch "
                        "in the repo, then exit (recovery after a crashed "
                        "launcher; also picks up live sessions' work)")
    p.add_argument("--image", default=None, metavar="IMAGE",
                   help="base OS image to launch. Overrides the repo's `image:` "
                        f"in {deps.DEPS_FILE}, which overrides the "
                        f"default ({DEFAULT_IMAGE}, Ubuntu 26.04 LTS)")
    p.add_argument("--cli", default=DEFAULT_CLI, choices=["incus", "lxc"],
                   help=f"container CLI (default: {DEFAULT_CLI})")
    p.add_argument("--name", help="session id and container name: letters, "
                   "digits, hyphens (default: Docker-style random, e.g. "
                   "quirky-hopper)")
    p.add_argument("--git-name", metavar="NAME",
                   help="git user.name inside the sandbox "
                        "(default: --github-user)")
    p.add_argument("--git-email", metavar="EMAIL",
                   default=os.environ.get("CLAUDE_SANDBOX_GIT_EMAIL")
                   or DEFAULT_GIT_EMAIL,
                   help="git user.email inside the sandbox (default: "
                        f"$CLAUDE_SANDBOX_GIT_EMAIL or {DEFAULT_GIT_EMAIL})")
    p.add_argument("--shell", action="store_true",
                   help="open a login shell instead of launching claude")
    p.add_argument("--no-final-check", action="store_true",
                   help="skip the repo's `check:` scripts (claude_sandbox_deps"
                        ".yaml) that run when you exit Claude, before "
                        "teardown, and reopen the session if they fail")
    p.add_argument("--tmux", action="store_true",
                   help="wrap the session in tmux inside the container "
                        "(mouse scrolling, 100k-line history)")
    p.add_argument("--nesting", action="store_true",
                   help="allow containers inside the sandbox "
                        "(security.nesting=true) and set up incus if the "
                        "repo's deps installed it")
    p.add_argument("--no-cache", action="store_true",
                   help="launch --image directly and install everything at "
                        "boot instead of using the cached base image")
    p.add_argument("--rebuild", action="store_true",
                   help="rebuild the cached base and deps images even if "
                        "they are fresh")
    p.add_argument("--max-age", type=float, default=7, metavar="DAYS",
                   help="a cached image older than this is stale: it is still "
                        "used (see --hard-max-age) and refreshed in the "
                        "background (default: 7)")
    p.add_argument("--hard-max-age", type=float, default=30, metavar="DAYS",
                   help="a cached image older than this is rebuilt before "
                        "launching; between --max-age and this it is used as "
                        "is while a background process refreshes it "
                        "(default: 30; 0 = always rebuild at --max-age)")
    p.add_argument("--refresh-images", action="store_true",
                   help="internal: rebuild stale cached images and exit (the "
                        "launcher runs this detached after launching on one)")
    p.add_argument("--model", metavar="MODEL",
                   default=os.environ.get("CLAUDE_SANDBOX_MODEL")
                   or DEFAULT_MODEL,
                   help="model for the Claude Code session: an alias "
                        "(opus, sonnet, haiku) or a full model ID "
                        f"(default: $CLAUDE_SANDBOX_MODEL or {DEFAULT_MODEL}); "
                        "'default' leaves the choice to Claude Code")
    p.add_argument("--disk-size", type=size_arg, default=DEFAULT_DISK_SIZE,
                   metavar="SIZE",
                   help="storage quota for the root disk of the sandbox and "
                        f"of image builds (default: {DEFAULT_DISK_SIZE}; 0 = "
                        "no limit; needs a pool with quota support)")
    p.add_argument("--volume-size", type=size_arg, default=DEFAULT_VOLUME_SIZE,
                   metavar="SIZE",
                   help="quota for each separate volume mounted over /tmp and "
                        "the clone directory, so a full one can't take down "
                        f"the OS (default: {DEFAULT_VOLUME_SIZE}; 0 = keep "
                        "everything on the root disk)")
    p.add_argument("--grow-pool", type=size_arg, metavar="SIZE",
                   help="first grow the incus storage pool to SIZE (loop-"
                        "backed pools only; changes the host, shared by all "
                        "sandboxes). Without it the launcher only checks "
                        "free space and tells you the command")
    p.add_argument("--no-deps", action="store_true",
                   help=f"don't install the repo's {deps.DEPS_FILE}")
    p.add_argument("--usb", action="append", default=[], metavar="VID:PID",
                   type=usb_spec,
                   help="pass the host USB device(s) with this id into the "
                        "sandbox (repeatable, e.g. 2e8a:000c). The repo's "
                        f"{host.HOST_FILE} can only request devices; "
                        "this flag, --usb-allow or a prompt grants them")
    p.add_argument("--no-usb", action="store_true",
                   help="attach no USB devices, ignoring --usb and the repo's "
                        "requests")
    p.add_argument("--usb-allow", dest="usb_allow", default=DEFAULT_USB_ALLOW,
                   metavar="PATH",
                   help="allowlist of repo-requested devices attached without "
                        "a prompt: lines of 'VID:PID [owner/repo]' "
                        f"(default: {DEFAULT_USB_ALLOW})")
    p.add_argument("--mount", action="append", default=[], metavar="SPEC",
                   type=mount_spec,
                   help="bind-mount a host directory into the sandbox "
                        "(repeatable): HOST[:CONTAINER][:ro|rw]. CONTAINER "
                        "defaults to /mnt/<name>; the mount is READ-ONLY "
                        "unless you add :rw, which lets the session change "
                        "your host files directly. Only this flag grants "
                        "mounts; the repo can't request them")
    p.add_argument("--claude-md", metavar="PATH",
                   default=os.path.join(paths.DATA, "sandbox_CLAUDE.md"),
                   help="template for ~/.claude/CLAUDE.md in the sandbox "
                        "(default: claude_sandbox/data/sandbox_CLAUDE.md)")
    p.add_argument("--setup-token", action="store_true",
                   help="run `claude setup-token` in a throwaway sandbox and "
                        "save the long-lived login token to --token-file, "
                        "then exit (no GitHub key, no clone)")
    p.add_argument("--token-file", default=DEFAULT_TOKEN_FILE, metavar="PATH",
                   help="Claude login token passed into every session "
                        f"(default: {DEFAULT_TOKEN_FILE}); overridden by "
                        "CLAUDE_CODE_OAUTH_TOKEN on the host")
    return p


def normalize_args(args):
    """Resolve the repository and defaults in place; ValueError on bad input.

    A repository is required unless --setup-token (no GitHub involved) or the
    explicit --account-key (an account-wide key is a much bigger grant, so it
    is never the silent result of forgetting the repo).
    """
    if args.repository and args.repo:
        raise ValueError("give the repository once: either REPOSITORY or --repo")
    if args.base and not args.local:
        raise ValueError("--base only applies with --local")
    if args.recover and not args.local:
        raise ValueError("--recover only applies with --local")
    if args.local:
        clash = [f for f, on in (("a repository", args.repository or args.repo),
                                 ("--account-key", args.account_key),
                                 ("--land-all", args.land_all),
                                 ("--read-only", args.read_only)) if on]
        if clash:
            raise ValueError(f"--local can't be combined with {', '.join(clash)}")
        args.local = resolve_local(args.local)
    if args.repo:
        args.repo = validate_repo(args.repo)
    elif args.repository:
        args.repo = resolve_repo(args.repository, args.github_user)
    if args.account_key and args.repo:
        raise ValueError("--account-key and a repository are mutually exclusive")
    if not (args.repo or args.local or args.account_key or args.setup_token
            or args.refresh_images):
        raise ValueError("a repository is required (or --account-key to use "
                         "an account-wide key, or --setup-token)")
    if args.land_all and not args.repo:
        raise ValueError("--land-all needs a repository")
    if args.name:
        sessions.validate_session(args.name)
    args.git_name = args.git_name or args.github_user
    return args


def mount_spec(text):
    """argparse type for --mount: a mounts.BindMount."""
    try:
        return mounts.parse_mount_spec(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def usb_spec(text):
    """argparse type for --usb: normalised (vid, pid) text 'vid:pid'."""
    try:
        return "%s:%s" % host.parse_usb_spec(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
