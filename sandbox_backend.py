"""sandbox_backend.py - where a session's code comes from and where its work goes.

The launcher's lifecycle (images, container, volumes, deps, USB, secrets, the
session itself) is the same whatever the source of the code. A `Backend`
supplies only the parts that differ:

- identity: what to call the source in logs, where its signing keys are kept;
- host checks before anything is built (e.g. which account `gh` is logged in as);
- reading a file from the source's default branch (the deps and host files);
- the network credential the container gets, if any, and revoking it;
- putting the code into the container, and getting the work back out;
- the text in the session's CLAUDE.md that describes all of this;
- landing: what happens to the session's branches once it is gone.

Implementations: `sandbox_backend_github.GitHubBackend` (deploy key, clone
over SSH, PRs) and `sandbox_backend_local.LocalBackend` (a git repo on the
host, bundles in and out, branches imported into it).
"""
import abc
from typing import Optional

from sandbox_containers import HOME


class Backend(abc.ABC):
    """One source of code for a session. Hooks that one backend doesn't need
    are no-ops here; the lifecycle in claude_sandbox calls every hook."""

    def __init__(self, args):
        self.args = args
        # For the user, printed at launch and again at exit; never put in
        # the session's CLAUDE.md.
        self.host_warnings: list = []

    # -- identity --------------------------------------------------------------
    kind = ""   # selects the `<!-- if KIND -->` blocks of the CLAUDE.md template

    @property
    @abc.abstractmethod
    def label(self) -> str:
        """The source as shown to people (logs, CLAUDE.md)."""

    @property
    def key_repo(self) -> Optional[str]:
        """`owner/name` id under which the session's public signing key is
        recorded (see sandbox_provenance), or None when nothing is signed."""
        return None

    @property
    def key_root(self) -> Optional[str]:
        """Key-store root for key_repo (None: sandbox_provenance's default)."""
        return None

    @property
    def has_repo(self) -> bool:
        """Whether there is a repository (with a deps file to read, a
        working copy to make)."""
        return True

    @property
    def can_push(self) -> bool:
        """Whether the session can push its work to a remote itself (else
        the launcher copies it out at the end)."""
        return False

    @property
    def has_commits(self) -> bool:
        """Whether the session makes commits that come back to the user (so
        they are signed and linted)."""
        return False

    def workdir(self) -> str:
        """Where the code lives in the container."""
        return HOME

    def keep_work(self, session_id) -> Optional[dict]:
        """Metadata for a work volume that must outlive the container until
        land() says its work is safe (None: an ordinary volume)."""
        return None

    work_kept = False   # set by the launcher: the work volume is a kept one

    # -- host side, before the container exists ----------------------------------
    def check_host(self) -> Optional[str]:
        """Fail-fast checks before anything is built: error text, or None."""
        return None

    def prefetch_plan(self) -> list:
        """Independent remote reads to start early (sandbox_github.start_prefetch)."""
        return []

    @abc.abstractmethod
    def read_file(self, path) -> Optional[bytes]:
        """A file at the root of the default branch (bytes), None if absent."""

    def preflight(self, live_names, own_name):
        """Advice and housekeeping before launch; must never block it."""

    # -- in the container ----------------------------------------------------------
    def provision(self, box, name, session_id) -> str:
        """Give the container its network credential, if any. Returns
        known_hosts lines to install ("" for none)."""
        return ""

    @abc.abstractmethod
    def populate(self, box, session_id) -> str:
        """Put the code in the container; return the working directory."""

    @abc.abstractmethod
    def access_lines(self, workdir, branch) -> list:
        """Markdown bullets for the session's CLAUDE.md "Access" section."""

    def trusted(self) -> bool:
        """Whether Claude Code may skip its folder-trust dialog. Fails closed."""
        return False

    def export(self, box, session_id):
        """Copy the work out while the container is still alive, after the
        session has exited and before teardown. Must never raise."""

    # -- after the session ---------------------------------------------------------
    def revoke(self):
        """Remove the container's credential (part of teardown). Must never
        raise."""

    def land(self, session_id) -> bool:
        """Bring the session's work back to the user, after teardown. ->
        whether a kept work volume may now be deleted. Must never raise."""
        return True
