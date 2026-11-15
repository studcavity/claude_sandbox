"""backends/local.py - the local backend: a git repo on the host.

`./run.sh --local PATH` works on a git repository on this machine, with no
GitHub (or any other remote) involved. See docs/specs/08-local-backend.md.

The trust boundary is a **git bundle** in each direction:
- in: the host bundles ONE branch of the primary repo (the user's own, so
  trusted) and streams it into the container, which clones it onto the
  size-capped work volume. Nothing is bind-mounted; the container never sees
  the primary's .git (refs, config, hooks).
- out: after the session, the container bundles its `sbx/<session>/*`
  branches and streams them to the host. The host fetches that FILE into the
  primary with fsck on and one explicit refspec per validated ref. Fetching
  from a bundle runs nothing from the producing repo (no config, no hooks, no
  upload-pack), so nothing the session wrote runs on the host, and no ref
  outside `refs/heads/sbx/<session>/` can be created or moved.

Pure decisions (ids, ref filtering, argv) are separate from the I/O so they
can be tested without a container.
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading

from claude_sandbox import provenance as provenance_mod
from claude_sandbox import sessions
from claude_sandbox.container import deps
from claude_sandbox.container import volumes
from claude_sandbox.github import land as github_land
from claude_sandbox.portable import check as portable_check
from claude_sandbox.portable import names
from claude_sandbox.portable import untrusted
from claude_sandbox.backends.base import Backend
from claude_sandbox.container.incus import HOME, Container
from claude_sandbox.util import log, run

BUNDLE_REF_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64}) (\S+)$")
# One path component of a branch name we accept back. Stricter than git's
# check-ref-format on purpose: these names reach the user's terminal and repo.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
MAX_REF_DEPTH = 8
MAX_BUNDLE_REFS = 100
DEFAULT_MAX_BUNDLE = 2 * 1024 ** 3   # bytes; bundles out are capped at this


class LocalError(RuntimeError):
    """A local-backend step failed; the message is safe to show the user."""


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def repo_id(path):
    """Stable `local/<name>-<hash>` id for the repo at `path` (already a
    realpath): the basename for people, a hash of the full path so two repos
    with one name don't share signing keys. Fits provenance_mod's
    `owner/name` rule (no `..`, limited alphabet)."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(path.rstrip("/")))
    base = re.sub(r"\.{2,}", ".", base).strip(".-")[:60] or "repo"
    digest = hashlib.sha256(path.encode()).hexdigest()[:8]
    return f"local/{base}-{digest}"


def key_root(environ=None):
    """Signing keys of local sessions live beside GitHub's, under their own
    root, so `local/<x>` can never collide with a GitHub owner called local."""
    return os.path.join(os.path.dirname(provenance_mod.store_root(environ)),
                        "signing-local")


# --------------------------------------------------------------------------- #
# what comes back
# --------------------------------------------------------------------------- #
def acceptable_ref(ref, session):
    """Whether `ref` (full name) may be imported for `session`: only
    refs/heads/sbx/<session>/<name>, every component of <name> plain."""
    prefix = "refs/heads/" + names.BRANCH_ROOT + f"/{session}/"
    if not names.SESSION_RE.match(session or "") or not ref.startswith(prefix):
        return False
    parts = ref[len(prefix):].split("/")
    return (0 < len(parts) <= MAX_REF_DEPTH
            and all(_COMPONENT_RE.match(p) and not p.endswith((".lock", "."))
                    and ".." not in p for p in parts))


def parse_heads(text, session):
    """`git bundle list-heads` output -> (accepted [(sha, ref)], rejected
    [text]). Anything that isn't a well-formed line for one of this session's
    branches is rejected (and never fetched); so is everything past
    MAX_BUNDLE_REFS."""
    accepted: list = []
    rejected: list = []
    seen: set = set()
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = BUNDLE_REF_RE.match(line.strip())
        if not m or not acceptable_ref(m.group(2), session) or m.group(2) in seen:
            rejected.append(line.strip()[:200])
            continue
        if len(accepted) >= MAX_BUNDLE_REFS:
            rejected.append(m.group(2))
            continue
        seen.add(m.group(2))
        accepted.append((m.group(1), m.group(2)))
    return accepted, rejected


def fetch_argv(primary, bundle, refs):
    """Fetch exactly `refs` from the bundle FILE into the same names in the
    primary. No `+`: a ref that already exists there only fast-forwards, so an
    import never rewrites what the user has since done to a branch. fsck
    rejects malformed objects."""
    if not refs:
        raise ValueError("no refs to fetch")
    for r in refs:
        if not r.startswith("refs/heads/"):
            raise ValueError(f"refusing to fetch {r!r}")
    return ["git", "-C", primary,
            "-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true",
            "fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
            os.path.abspath(bundle), *[f"{r}:{r}" for r in refs]]


def bundle_out_argv(workdir, session, base_sha):
    """Run in the container: this session's branches, minus what the primary
    already has (the base commit), to stdout."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha or ""):
        raise ValueError(f"not a commit id: {base_sha!r}")
    if not names.SESSION_RE.match(session or ""):
        raise ValueError(f"bad session name {session!r}")
    return ["git", "-C", workdir, "bundle", "create", "-",
            f"--branches={names.BRANCH_ROOT}/{session}/*", f"^{base_sha}"]


def bundle_in_argv(primary, branch):
    """Run on the host: one branch of the primary, to stdout."""
    return ["git", "-C", primary, "bundle", "create", "-",
            f"refs/heads/{branch}"]


def copy_capped(src, dst, limit, chunk=1 << 20):
    """Copy a stream until EOF; raise LocalError once more than `limit` bytes
    arrive (the caller deletes the partial file). -> bytes copied."""
    total = 0
    while True:
        buf = src.read(chunk)
        if not buf:
            return total
        total += len(buf)
        if total > limit:
            raise LocalError(f"bundle is larger than the {limit}-byte cap")
        dst.write(buf)


# --------------------------------------------------------------------------- #
# I/O on the host
# --------------------------------------------------------------------------- #
GIT_TIMEOUT = 600          # seconds for one bundle in or out
BUNDLE_IN = ".local/share/claude-sandbox/base.bundle"   # under the session's home


def host_git(*args, cwd, check=True, text=True):
    """git on the host, in the user's own (trusted) primary repo. Never point
    this at anything the session wrote."""
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                       text=text, timeout=GIT_TIMEOUT)
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode(errors="replace")
        raise LocalError(f"git {' '.join(args[:2])} failed in {cwd}: "
                         f"{err.strip()[-300:]}")
    return r


def resolve_local(path):
    """--local PATH -> the realpath of its repository's top level. Raises
    ValueError (shown as a usage error) when it isn't a git work tree."""
    real = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(real):
        raise ValueError(f"--local: no such directory: {path}")
    r = subprocess.run(["git", "-C", real, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        raise ValueError(f"--local: {path} is not inside a git work tree")
    return os.path.realpath(r.stdout.strip())


def resolve_base(primary, wanted):
    """(branch, commit) to start from: --base, else the checked-out branch."""
    branch = wanted
    if not branch:
        r = host_git("symbolic-ref", "-q", "--short", "HEAD", cwd=primary,
                     check=False)
        branch = r.stdout.strip()
        if r.returncode != 0 or not branch:
            raise LocalError(f"{primary} has a detached HEAD: pass --base BRANCH")
    if host_git("check-ref-format", "--branch", branch, cwd=primary,
                check=False).returncode != 0 or branch.startswith("-"):
        raise LocalError(f"--base: {branch!r} is not a valid branch name")
    r = host_git("rev-parse", "--verify", "-q", "--end-of-options",
                 f"refs/heads/{branch}^{{commit}}", cwd=primary, check=False)
    if r.returncode != 0:
        raise LocalError(f"{primary} has no branch {branch!r}")
    return branch, r.stdout.strip()


def state_dir(environ=None):
    """Where bundles on their way out are kept (beside the key store)."""
    return os.path.join(os.path.dirname(provenance_mod.store_root(environ)),
                        "local")


def stream_into(producer, consumer, timeout=GIT_TIMEOUT):
    """Pipe producer's stdout into consumer's stdin (argv lists, no shell),
    never holding the whole stream in memory. Raises LocalError naming the
    side that failed."""
    with tempfile.TemporaryFile() as perr:
        p = subprocess.Popen(producer, stdout=subprocess.PIPE, stderr=perr)
        try:
            c = subprocess.run(consumer, stdin=p.stdout, capture_output=True,
                               timeout=timeout)
        finally:
            assert p.stdout is not None
            p.stdout.close()
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        perr.seek(0)
        if p.returncode != 0:
            raise LocalError(f"{' '.join(producer[:1] + producer[3:5])} failed: "
                             f"{perr.read().decode(errors='replace').strip()[-300:]}")
    if c.returncode != 0:
        raise LocalError("copying into the container failed: "
                         f"{c.stderr.decode(errors='replace').strip()[-300:]}")


def stream_out(producer, dest, limit, timeout=GIT_TIMEOUT):
    """Run producer and write its stdout to dest (mode 0600), at most `limit`
    bytes and `timeout` seconds. -> (returncode, stderr text). A failure or
    overrun leaves no file behind."""
    tmp = dest + ".part"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with tempfile.TemporaryFile() as perr, os.fdopen(fd, "wb") as out:
        p = subprocess.Popen(producer, stdout=subprocess.PIPE, stderr=perr)
        timer = threading.Timer(timeout, p.kill)
        timer.start()
        try:
            assert p.stdout is not None
            copy_capped(p.stdout, out, limit)
        except LocalError:
            p.kill()
            p.wait()
            os.unlink(tmp)
            raise
        finally:
            timer.cancel()
        rc = p.wait()
        perr.seek(0)
        err = perr.read().decode(errors="replace")
    if rc != 0:
        os.unlink(tmp)
        return rc, err
    os.replace(tmp, dest)
    return 0, err


# --------------------------------------------------------------------------- #
class LocalBackend(Backend):
    """A git repository on the host. Nothing the session writes is ever
    run, or read by git as a repository, on the host: only the bundle FILE it
    produces, through fetch_argv."""
    kind = "local"

    def __init__(self, args, environ=None, home=HOME):
        super().__init__(args)
        self.home = home                   # the session user's, in the container
        self.primary = args.local          # resolve_local()'d by normalize_args
        self.id = repo_id(self.primary)
        self.environ = environ
        self.base: str = ""
        self.base_sha: str = ""            # pinned by resolve()
        self.session_id = ""               # set by populate
        self.exported = None               # bundle path once export succeeds
        self.export_failed = False

    # -- identity --------------------------------------------------------------
    @property
    def label(self):
        return f"local:{self.primary}"

    @property
    def key_repo(self):
        return self.id

    @property
    def key_root(self):
        return key_root(self.environ)

    @property
    def has_commits(self):
        return True

    def workdir(self):
        return f"{self.home}/{self.id.split('/', 1)[1].rsplit('-', 1)[0]}"

    def keep_work(self, session_id):
        """Everything `--recover` needs to export and import the work if the
        launcher never gets to (set by the host; the container can't change a
        volume's config)."""
        return {"source": self.primary, "session": session_id,
                "base": self.resolve(), "workdir": self.workdir()}

    # -- host side ---------------------------------------------------------------
    def resolve(self):
        """Pin the base branch and commit once; everything else reads from
        that commit, so a branch moving during the launch changes nothing."""
        if not self.base_sha:
            self.base, self.base_sha = resolve_base(self.primary, self.args.base)
        return self.base_sha

    def check_host(self):
        try:
            sha = self.resolve()
        except (LocalError, OSError, subprocess.SubprocessError) as e:
            return str(e)
        log(f"local repo {self.primary}: starting from {self.base} ({sha[:12]})")
        dirty = host_git("status", "--porcelain", "--untracked-files=no",
                         cwd=self.primary, check=False).stdout.strip()
        if dirty:
            log("warning: the local repo has uncommitted changes; only "
                f"commits on {self.base} go into the sandbox")
        return None

    def read_file(self, path):
        """From the base commit (never the work tree, never another branch)."""
        spec = f"{self.resolve()}:{path}"
        if host_git("cat-file", "-e", spec, cwd=self.primary,
                    check=False).returncode != 0:
            return None
        return host_git("cat-file", "blob", spec, cwd=self.primary,
                        text=False).stdout

    # -- in the container ----------------------------------------------------------
    def populate(self, box, session_id):
        """Bundle the base branch in over stdin, clone it, and start the
        session's work branch. `origin` stays the bundle, so origin/HEAD is
        the base and sandbox-check works offline."""
        workdir = self.workdir()
        self.resolve()
        self.session_id = session_id
        log(f"copying {self.base} of {self.primary} into the sandbox")
        bundle = f"{self.home}/{BUNDLE_IN}"
        box.user("mkdir -p " + shlex.quote(os.path.dirname(bundle)))
        stream_into(bundle_in_argv(self.primary, self.base),
                    box.user_cmd("sh", "-c", 'cat > "$1"', "sh", bundle))
        box.user_argv(
            ["sh", "-c",
             'git clone -q -b "$1" -- "$2" "$3" && cd "$3" && '
             'git checkout -q -b "$4" && git remote set-head origin "$1"',
             "sh", self.base, bundle, workdir,
             sessions.work_branch(session_id)], capture=True)
        return workdir

    def access_lines(self, workdir, branch):
        return [
            f"- Working copy: `{workdir}`, a copy of the `{self.base}` branch "
            "of a git repository on the user's machine"
            + (f" (you are on `{branch}`)" if branch else "") + ". `origin` "
            "is a bundle file of that branch: `origin/HEAD` is the base, and "
            "`git fetch` brings nothing new.",
            "- There is **no remote you can push to** and no credential for "
            "GitHub or any other service. When the session ends, the "
            "launcher copies your committed branches under "
            f"`{sessions.branch_prefix(self.session_id)}` into the "
            "user's repository for review; nothing else leaves.",
            f"- `{workdir}` is a volume that outlives this container: if the "
            "container dies, the user can still recover your committed "
            "branches from it." if self.work_kept else
            f"- `{workdir}` is on the container's own disk: if the container "
            "dies before the session ends, your work is lost.",
        ]

    def trusted(self):
        return True   # the user named this directory themselves

    def export(self, box, session_id):
        """Bundle this session's branches out of the live container."""
        try:
            d = os.path.join(state_dir(self.environ), *self.id.split("/"))
            os.makedirs(d, mode=0o700, exist_ok=True)
            dest = os.path.join(d, f"{session_id}.bundle")
            argv = box.user_cmd(*bundle_out_argv(self.workdir(), session_id,
                                                 self.base_sha))
            rc, err = stream_out(argv, dest, DEFAULT_MAX_BUNDLE)
            if rc == 0:
                self.exported = dest
                log(f"exported the session's branches ({os.path.getsize(dest)} "
                    "bytes)")
            elif "empty bundle" in err:
                log(f"no new commits on {sessions.branch_prefix(session_id)}"
                    "*: nothing to import")
            else:
                self.export_failed = True
                log(f"warning: couldn't export the session's branches: "
                    f"{err.strip()[-300:]}")
        except Exception as e:  # never stop teardown
            self.export_failed = True
            log(f"warning: couldn't export the session's branches: {e}")

    # -- after the session ---------------------------------------------------------
    def land(self, session_id):
        """Import the exported bundle into the primary, then report. ->
        whether the kept work volume may go. Never raises."""
        if not self.exported:
            if self.export_failed:
                self.recovery_hint()
                return False
            return True   # nothing new to import
        try:
            imported = import_bundle(self.primary, self.exported, session_id)
        except (LocalError, OSError, subprocess.SubprocessError) as e:
            log(f"warning: couldn't import the session's branches: {e}; "
                f"the bundle is kept at {self.exported}")
            self.recovery_hint()
            return False
        try:
            print("\n" + report(self.primary, self.base, self.base_sha,
                                session_id, imported,
                                key=provenance_mod.load_key(
                                    self.id, session_id, self.key_root)),
                  file=sys.stderr)
        except Exception as e:
            log(f"warning: couldn't build the review report: {e}")
        os.unlink(self.exported)
        return True

    def recovery_hint(self):
        if self.work_kept:
            log("the session's work is kept on its volume; import it with: "
                f"./run.sh --local {shlex.quote(self.primary)} --recover")
        else:
            log("warning: the work directory was not a kept volume, so the "
                "session's unexported work is lost")

def import_bundle(primary, bundle, session):
    """Fetch this session's branches from a bundle file into the primary.
    -> [(ref, tip)] imported. Refs the bundle carries for anything else are
    reported and skipped."""
    heads = host_git("bundle", "list-heads", bundle, cwd=primary).stdout
    accepted, rejected = parse_heads(heads, session)
    for r in rejected:
        log(f"warning: ignored a ref in the session's bundle: "
            f"{untrusted.one_line(r, 200)}")
    if not accepted:
        return []
    host_git("bundle", "verify", "-q", bundle, cwd=primary)
    refs = [r for _, r in accepted]
    r = subprocess.run(fetch_argv(primary, bundle, refs), capture_output=True,
                       text=True, timeout=GIT_TIMEOUT)
    if r.returncode != 0:
        raise LocalError(f"git fetch from the bundle failed: "
                         f"{r.stderr.strip()[-400:]}")
    return accepted_refs_after(primary, accepted)


def accepted_refs_after(primary, accepted):
    """[(ref, tip)] as they now stand in the primary."""
    out = []
    for sha, ref in accepted:
        tip = host_git("rev-parse", "--verify", "-q", ref, cwd=primary,
                       check=False).stdout.strip()
        if tip:
            out.append((ref, tip))
    return out


# --------------------------------------------------------------------------- #
# the review report
# --------------------------------------------------------------------------- #
def setup_paths(primary, base_sha):
    """`setup:` script paths in the base's deps file ([] if none or invalid:
    an invalid file aborts every launch, so nothing in it would run)."""
    r = host_git("cat-file", "blob", f"{base_sha}:{deps.DEPS_FILE}",
                 cwd=primary, check=False, text=False)
    if r.returncode != 0:
        return []
    try:
        return [x["path"] for x in deps.parse(r.stdout).get("setup") or []]
    except deps.DepsError:
        return []


def provenance(primary, base_sha, tip, key, session):
    """(ok, text): every commit in base..tip signed by the session's key."""
    if key is None:
        return False, f"no signing key recorded for session {session}"
    with tempfile.TemporaryDirectory() as tmp:
        signers = os.path.join(tmp, "allowed_signers")
        with open(signers, "w") as f:
            f.write(provenance_mod.allowed_signers([key]))
        commits = provenance_mod.read_commits(base_sha, tip, signers, primary)
    problems = provenance_mod.judge(
        commits, {provenance_mod.fingerprint(key): key})
    return not problems, provenance_mod.describe(commits, problems, session)


def branch_facts(primary, base_sha, session, ref, tip, key, setup=()):
    """Everything the report says about one imported branch (I/O: host git
    on the primary, reading the imported commits as data)."""
    log_out = host_git("log", "-z", "--reverse", "--format=%h%x1f%B",
                       f"{base_sha}..{tip}", cwd=primary).stdout
    msgs = [tuple(rec.split("\x1f", 1)) for rec in log_out.split("\0")
            if "\x1f" in rec]
    files = host_git("diff", "--name-only", "-z", "--no-renames", base_sha, tip,
                     cwd=primary).stdout.split("\0")
    try:
        prov_ok, prov = provenance(primary, base_sha, tip, key, session)
    except (provenance_mod.ProvenanceError, subprocess.SubprocessError,
            OSError) as e:
        prov_ok, prov = False, f"couldn't check: {e}"
    try:
        found = portable_check.check_findings(base_sha, tip, cwd=primary)
        errors = sum(1 for f in found if f.severity == portable_check.ERROR)
    except Exception as e:  # advisory only
        errors = -1
        log(f"warning: the commit checker failed on {ref}: {e}")
    return dict(branch=ref.removeprefix("refs/heads/"), commits=len(msgs),
                provenance_ok=prov_ok, provenance=prov, checker_errors=errors,
                sensitive=github_land.sensitive_paths(
                    [f for f in files if f], extra=setup),
                synopsis=github_land.synopsis(msgs),
                not_verified=github_land.not_verified_lines(msgs))


def format_report(primary, base, session, rows):
    """The exit summary of an import (pure). Every piece of session text is
    sanitised; branch names were validated before the import."""
    clean = github_land.safe_line
    out = [f"\u250c branches imported from session {session} into {primary}"]
    for r in rows:
        mark = "\u2714" if r["provenance_ok"] and r["checker_errors"] == 0 else "\u26a0"
        n = r["commits"]
        out.append(f"\u2502 {mark} {r['branch']}  {n} commit{'s' * (n != 1)}  "
                   f"provenance: {clean(r['provenance'], 140)}")
        if r["synopsis"]:
            out.append(f"\u2502     PR-Title (from the session): "
                       f"{clean(r['synopsis'][1], 120)}")
        if r["sensitive"]:
            out.append("\u2502     \u26a0 touches files that run with privilege "
                       "(review first): "
                       + clean(", ".join(r["sensitive"][:10]), 300))
        if r["checker_errors"] > 0:
            out.append(f"\u2502     \u26a0 commit checker: {r['checker_errors']} "
                       "error(s); run scripts/sandbox_check.py in the repo for details")
        elif r["checker_errors"] < 0:
            out.append("\u2502     \u26a0 commit checker couldn't run")
        for sha, line in r["not_verified"][:github_land.MAX_CAVEATS]:
            out.append(f"\u2502     {clean(sha, 12)}: {clean(line, 160)}")
    if not rows:
        out.append("\u2502 nothing imported")
        out.append("\u2514 the launcher never touches any other branch.")
    else:
        out.append(f"\u2514 review: git -C {shlex.quote(primary)} log -p "
                   f"{shlex.quote(base)}..{rows[0]['branch']}  "
                   "(merging is yours; nothing else was touched)")
    return "\n".join(out)


def report(primary, base, base_sha, session, imported, key):
    setup = setup_paths(primary, base_sha)
    return format_report(primary, base, session, [
        branch_facts(primary, base_sha, session, ref, tip, key, setup)
        for ref, tip in imported])


# --------------------------------------------------------------------------- #
# --recover: import what a crashed launch left on a kept volume
# --------------------------------------------------------------------------- #
def kept_volumes(volumes_json, primary):
    """Kept work volumes of `primary` from `storage volume list --format
    json` -> [dict(name, session, base, workdir, used_by)]. Entries whose
    labels don't validate are skipped: they decide paths and refs."""
    meta = volumes.META_PREFIX
    out = []
    for v in json.loads(volumes_json or "[]"):
        cfg = v.get("config") or {}
        name = v.get("name", "")
        session, base = cfg.get(meta + "session", ""), cfg.get(meta + "base", "")
        workdir = cfg.get(meta + "workdir", "")
        if (v.get("type") != "custom"
                or not name.startswith(volumes.KEEP_PREFIX)
                or cfg.get(meta + "source") != primary
                or not names.SESSION_RE.match(session)
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base)
                or not re.fullmatch(r"(/[A-Za-z0-9._-]+){2,}", workdir)
                or os.path.normpath(workdir) != workdir or ".." in workdir):
            continue
        out.append(dict(name=name, session=session, base=base, workdir=workdir,
                        used_by=list(v.get("used_by") or [])))
    return out


def recover(args, make_image, environ=None):
    """For each kept volume of this repo that no instance uses: mount it
    read-only in a throwaway helper container, bundle the session's branches
    out there (the host never runs git on the volume), import them, and
    delete the volume once that worked. -> exit status."""
    cli, primary = args.cli, args.local
    pool = volumes.default_pool(cli)
    found = kept_volumes(run([cli, "storage", "volume", "list", pool,
                              "--format", "json"], capture=True).stdout, primary)
    if not found:
        log(f"no kept work volumes for {primary}")
        return 0
    status, image = 0, None
    for v in found:
        if v["used_by"]:
            log(f"{v['name']} is still in use by {', '.join(v['used_by'])}; "
                f"stop that first (`{cli} stop ...`)")
            status = 1
            continue
        be = LocalBackend(args, environ, home=os.path.dirname(v["workdir"]))
        be.base, be.base_sha = v["base"][:12], v["base"]
        if be.workdir() != v["workdir"]:
            log(f"warning: {v['name']} was made for {v['workdir']}, not "
                f"{be.workdir()}; skipped")
            status = 1
            continue
        image = image or make_image()
        helper = Container(cli, f"cs-recover-{v['session']}"[:63])
        log(f"recovering session {v['session']} from {v['name']}")
        try:
            helper.launch(image, ephemeral=True)
            run([cli, "config", "device", "add", helper.name, "cs-work", "disk",
                 f"pool={pool}", f"source={v['name']}", f"path={v['workdir']}",
                 "readonly=true"], capture=True)
            be.export(helper, v["session"])
        except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
            log(f"warning: couldn't start the recovery container: {e}")
            be.export_failed = True
        finally:
            helper.delete()
        be.work_kept = True
        if be.land(v["session"]):
            run([cli, "storage", "volume", "delete", pool, v["name"]],
                check=False, capture=True)
            log(f"deleted {v['name']}")
        else:
            status = 1
    return status
