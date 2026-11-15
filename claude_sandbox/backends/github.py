"""backends/github.py - the GitHub backend: deploy key, clone, PRs.

The session gets a per-session SSH deploy key on one repo (or, with
--account-key, an account key), clones over SSH, and pushes `sbx/<session>/*`
branches. After teardown the launcher opens a PR for each and posts their
`sandbox/provenance` status. Before launch it checks (and, after a yes,
tightens) the repo's protection and Actions settings; what it finds is told to
the user only (`host_warnings`), never to the session.

Every GitHub call goes through the host's `gh` login (sandbox_github).
"""
import functools
import os
import shlex
import subprocess
import sys

from claude_sandbox import claude_config as sandbox_claude_config
from claude_sandbox import paths
from claude_sandbox.container import deps as sandbox_deps
from claude_sandbox.github import hardening as sandbox_hardening
from claude_sandbox.container import host as sandbox_host
from claude_sandbox.github import land as sandbox_land
from claude_sandbox.github import protect as sandbox_protect
from claude_sandbox import provenance as sandbox_provenance
from claude_sandbox import sessions as sandbox_sessions
from claude_sandbox.backends.base import Backend
from claude_sandbox.container.incus import HOME
from claude_sandbox.github.gh import (GitHubError, gh_api, github_login, repo_owner,
                            token_scopes, workflow_push_warning)
from claude_sandbox.util import log



# --------------------------------------------------------------------------- #
# pure helpers and prompts
# --------------------------------------------------------------------------- #
def repo_workdir(repo):
    return f"{HOME}/{repo.split('/')[1]}" if repo else HOME


def access_lines(workdir, repo, branch, read_only):
    if not repo:
        return [f"- Working directory: `{workdir}`. No repo was cloned.",
                "- An SSH key is registered on the user's GitHub "
                "**account** for this session, so you can clone and "
                "push to any repo they can access."]
    return [
        f"- Working copy: `{workdir}`, a clone of `github.com/{repo}`"
        + (f" (branch `{branch}`)" if branch else "") + ".",
        f"- GitHub access is an SSH **deploy key scoped to `{repo}` "
        "only**. Other repos can't be cloned or pushed over SSH. "
        "`https://github.com/` URLs are rewritten to SSH.",
        "- The deploy key is **read-only**: you cannot push. Tell the "
        "user early that commits will be lost when the sandbox ends "
        "unless they export them some other way."
        if read_only else
        "- The deploy key has write access, so you can push.",
    ]


def check_expected_user(login, expected):
    """Error text when gh is logged in as someone unexpected, else None."""
    if login.lower() == expected.lower():
        return None
    return (f"gh is authenticated as {login!r}, expected {expected!r} "
            "(--github-user); refusing to continue. Switch accounts with "
            "`gh auth switch`, or pass --github-user / --no-user-check if "
            "that is intended.")


def prefetch_plan(args):
    """The independent GitHub reads a launch makes, to start together (see
    sandbox_github.start_prefetch). Mirrors what the launch will actually ask
    for, so nothing is fetched that a flag turns off."""
    if not args.repo or args.setup_token or args.land_all or args.no_prefetch:
        return []
    repo = args.repo
    specs: list = [("login",)]
    if not args.no_deps:
        specs.append(("api", f"/repos/{repo}/contents/{sandbox_deps.DEPS_FILE}", True))
    if not args.no_usb:
        specs.append(("api", f"/repos/{repo}/contents/{sandbox_host.HOST_FILE}", True))
    if not args.read_only:
        specs += [("scopes",), ("api", f"/repos/{repo}", False),
                  ("api", f"/repos/{repo}/keys?per_page=100", False),
                  ("api", f"/repos/{repo}/actions/permissions/workflow", False),
                  ("api", f"/repos/{repo}/actions/secrets?per_page=1", False),
                  ("api", f"/repos/{repo}/actions/organization-secrets?per_page=1",
                   False)]
    return specs


def ask_hardening(repo, cond):
    """Interactive yes/no (default no) to change one Actions setting, showing
    the exact call it would make."""
    print(f"\n{repo}: {cond.text}.\nThe fix would make this call:\n  "
          f"{sandbox_hardening.describe_fix(cond.fix)}", file=sys.stderr)
    try:
        return input("Apply it? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask_protect(repo, branch, effects):
    """Interactive yes/no (default no) to apply the protection ruleset."""
    print(f"\n\033[1m{repo}: `{branch}` does not require pull requests.\033[0m"
          "\nApply the protection ruleset? It would:", file=sys.stderr)
    for e in effects:
        print(f"  - {e}", file=sys.stderr)
    try:
        return input("Apply branch protections to this repo? [y/N] "
                     ).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask_ruleset(repo, name, effects):
    """Interactive yes/no (default no) to apply a ruleset the repo defines."""
    print(f"\n\033[1m{repo} defines ruleset `{name}` "
          "(rulesets/ on its default branch), not applied yet.\033[0m"
          "\nApplying it would:", file=sys.stderr)
    for e in effects:
        print(f"  - {e}", file=sys.stderr)
    try:
        return input(f"Apply ruleset `{name}` to this repo? [y/N] "
                     ).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def ask_checks(branch, checks):
    """Interactive yes/no (default no) to also require these CI checks."""
    print("\nThese checks passed on the latest commit of "
          f"`{branch}`:", file=sys.stderr)
    for c in checks:
        print(f"  - {c}", file=sys.stderr)
    print("Requiring them also makes PRs wait for them, and (strict mode) "
          "need to be up to date with the base. A name that never reports "
          "would block every merge.", file=sys.stderr)
    try:
        return input("Require these checks too? [y/N] ").strip().lower() in (
            "y", "yes")
    except EOFError:
        return False


def confirm_held(branch, detail):
    """Ask whether to open a PR for a branch held for touching privileged
    files. Default no; no answer (EOF) is no. Both strings are session text,
    so they are sanitised before they reach the terminal."""
    print(f"\n⚠ {sandbox_land.clean_text(branch, 120)} touches "
          f"{sandbox_land.clean_text(detail, 300)}\nThese run with extra "
          "privilege once merged or on CI; review the diff first.",
          file=sys.stderr)
    try:
        return input("Open a PR for it anyway? [y/N] ").strip().lower() in (
            "y", "yes")
    except EOFError:
        return False


def report_landing(results, session=None):
    """Print the exit summary of PRs the launcher opened (to stderr)."""
    print("\n" + sandbox_land.format_landing(
        results, session, color=sys.stderr.isatty()), file=sys.stderr)


def report_provenance(outcomes):
    """Print the exit summary of `sandbox/provenance` statuses (to stderr)."""
    text = sandbox_provenance.format_outcomes(outcomes)
    if text:
        print("\n" + text, file=sys.stderr)


def land_and_report(args, session=None, open_prs=True):
    """The one post-landing sequence, for a session's branches (every
    session's when `session` is None): open PRs, then post their provenance.
    Each step has its own try, so one failing never stops the next, and
    nothing raises. -> (landed, ok), where ok is False if landing failed."""
    who = session or "all sessions"
    landed, ok = [], True
    if open_prs:
        try:
            landed = sandbox_land.land(
                args.repo, session, confirm_held if sys.stdin.isatty() else None)
            report_landing(landed, session)
        except Exception as e:
            ok = False
            log(f"warning: couldn't open PRs for {who}: {e}"
                + (f"; push state is on GitHub under "
                   f"{sandbox_sessions.branch_prefix(session)}" if session else ""))
    if landed and not args.no_provenance:
        try:
            report_provenance(sandbox_provenance.post_session(args.repo, landed))
        except Exception as e:
            log(f"warning: couldn't post {sandbox_provenance.CONTEXT} "
                f"for {who}: {e}")
    return landed, ok


def check_workflow_scope():
    """Warn (never fail) if the sandbox WILL be able to push workflow files."""
    try:
        msg = workflow_push_warning(token_scopes())
    except (GitHubError, OSError) as e:
        msg = workflow_push_warning(None) + f" ({e})"
    if msg:
        log(f"warning: {msg}")


def land_all(args):
    """--land-all: open PRs for every session's unmerged sbx/* branch.
    -> exit status."""
    try:
        if not args.no_user_check:
            bad = check_expected_user(github_login(), args.github_user)
            if bad:
                print(f"error: {bad}", file=sys.stderr)
                return 1
        _, ok = land_and_report(args)
        return 0 if ok else 1
    except GitHubError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


# --------------------------------------------------------------------------- #
class GitHubBackend(Backend):
    kind = "github"

    def __init__(self, args):
        super().__init__(args)
        self.key_id = None

    @functools.cached_property
    def login(self):
        # Looked up on first use so --setup-token works without GitHub auth.
        # Also proves `gh` is installed and logged in, before anything is built.
        return github_login()

    # -- identity --------------------------------------------------------------
    @property
    def label(self):
        return self.args.repo or "(no repo)"

    @property
    def key_repo(self):
        return self.args.repo

    @property
    def has_repo(self):
        return bool(self.args.repo)

    @property
    def can_push(self):
        return self.has_commits

    @property
    def has_commits(self):
        return bool(self.args.repo and not self.args.read_only)

    def workdir(self):
        return repo_workdir(self.args.repo)

    # -- host side ---------------------------------------------------------------
    def check_host(self):
        try:
            login = self.login  # fail fast, before building or launching anything
        except GitHubError as e:
            return str(e)
        if not self.args.no_user_check:
            bad = check_expected_user(login, self.args.github_user)
            if bad:
                return bad
        if not self.args.read_only:
            check_workflow_scope()
        return None

    def prefetch_plan(self):
        return prefetch_plan(self.args)

    def read_file(self, path):
        if not self.args.repo:
            return None
        try:
            return gh_api("GET", f"/repos/{self.args.repo}/contents/{path}",
                          raw=True)
        except GitHubError as e:
            if e.code != 404:
                raise
            return None

    def preflight(self, live_names, own_name):
        """Advice and housekeeping before launch; never blocks the launch."""
        a = self.args
        if not a.repo or a.read_only:
            return
        self.protect_default_branch()
        self.harden_actions()
        try:
            gone = sandbox_sessions.reap_stale_keys(
                a.repo, sandbox_sessions.host_id(), live_names() | {own_name})
            for key_id in gone:
                log(f"removed stale deploy key {key_id} (its container is gone)")
        except (GitHubError, subprocess.CalledProcessError, OSError) as e:
            log(f"warning: couldn't reap stale deploy keys: {e}")

    def protect_default_branch(self):
        """Check that the default branch requires PRs; if it doesn't, offer
        (on a terminal, default no) to apply rulesets/protect-main.json.
        Advice only: never blocks the launch. What is still missing afterwards
        becomes host-side warnings (self.host_warnings: printed now and in the
        exit summary); nothing about the repo's protection reaches the
        session, whatever its state."""
        a = self.args
        branch, state = "the default branch", "unknown"
        try:
            branch, warn, state = sandbox_sessions.check_protection(a.repo)
            if (state in ("unprotected", "protected")
                    and not a.no_protect_prompt and sys.stdin.isatty()):
                state, _ = self.offer_protection(branch, state)
        except (GitHubError, OSError, ValueError) as e:
            log(f"warning: couldn't check or apply branch protection: {e}")
        required = ci = None
        if state in ("protected", "applied"):
            required = sandbox_protect.required_on(a.repo, branch)
            ci = sandbox_protect.discover_checks(a.repo, branch)
        self.add_host_warnings(sandbox_protect.gap_warnings(
            a.repo, branch, state, required, ci or []))

    def harden_actions(self):
        """Report whether the repo's Actions settings keep session code's CI
        runs untrusted (read-only token, Actions can't approve PRs, no secrets
        to reach) and, on a terminal and after a yes each, fix the settable
        ones (see sandbox_hardening). Advice only: never blocks the launch;
        what is still unmet becomes host-side warnings, never session text."""
        a = self.args
        ask = None
        if not a.no_hardening_prompt and sys.stdin.isatty():
            ask = lambda cond: ask_hardening(a.repo, cond)  # noqa: E731
        try:
            found, notes = sandbox_hardening.harden(a.repo, gh_api, ask)
        except (GitHubError, OSError, ValueError) as e:
            log(f"warning: couldn't check the repo's Actions settings: {e}")
            return
        log("Actions settings (no same-repo approval setting exists on "
            "GitHub; these keep session code's CI runs untrusted):")
        for line in notes + sandbox_hardening.summary_lines(found):
            log(f"  {line}")
        self.add_host_warnings(sandbox_hardening.warnings(found, bool(ask)),
                               show=False)

    def add_host_warnings(self, warnings, show=True):
        """Keep each for the exit summary, and show it now unless already
        shown."""
        for w in warnings:
            if show:
                log(f"warning: {w}")
        self.host_warnings.extend(warnings)

    def offer_protection(self, branch, state="unprotected"):
        """Ask, then apply. Returns (state, required_checks).

        Rulesets the repo defines itself (rulesets/*.json on its default
        branch) win over the launcher's generic one and are offered even when
        the branch is already protected (they may add e.g. a required check).
        """
        a = self.args
        repo_rs, problems = sandbox_protect.fetch_repo_rulesets(a.repo, branch)
        for p in problems:
            log(f"warning: ignoring {p}")
        existing = sandbox_protect.existing_ruleset_names(a.repo)
        if repo_rs:
            return self.offer_repo_rulesets(
                branch, state, sandbox_protect.pending(repo_rs, existing))
        if state != "unprotected":
            return state, []
        ruleset = sandbox_protect.load_ruleset(
            os.path.join(paths.RULESETS, "protect-main.json"))
        if ruleset["name"] in existing:
            log(f"ruleset {ruleset['name']!r} already exists but doesn't "
                "require PRs on the default branch; fix it in Settings -> Rules")
            return "unprotected", []
        if not ask_protect(a.repo, branch, sandbox_protect.describe(ruleset,
                                                                    branch)):
            return "unprotected", []
        checks = sandbox_protect.discover_checks(a.repo, branch)
        if checks and not ask_checks(branch, checks):
            checks = []
        sandbox_protect.apply_ruleset(
            a.repo, sandbox_protect.with_checks(ruleset, checks))
        log(f"applied ruleset {ruleset['name']!r} to {a.repo}"
            + (f", requiring: {', '.join(checks)}" if checks else
               " (no required status checks)"))
        return "applied", checks

    def offer_repo_rulesets(self, branch, state, todo):
        """Offer each not-yet-applied repo-defined ruleset, one yes/no each.
        Returns (state, required_checks of the ones applied)."""
        a = self.args
        checks = set()
        for rs in todo:
            if not ask_ruleset(a.repo, rs["name"],
                               sandbox_protect.describe_rules(rs, branch)):
                continue
            sandbox_protect.apply_ruleset(a.repo, rs)
            checks.update(sandbox_protect.required_checks(rs))
            log(f"applied ruleset {rs['name']!r} to {a.repo}")
            if state == "unprotected" and any(
                    r["type"] == "pull_request" for r in rs["rules"]):
                state = "applied"
        return state, sorted(checks)

    # -- in the container ----------------------------------------------------------
    def provision(self, box, name, session_id):
        """An SSH key made inside the container (the private half never
        leaves it), registered as a deploy key (or an account key), and
        GitHub's host keys pinned from the API."""
        log("generating SSH key inside the container")
        # One exec makes the key and prints its public half.
        pubkey = box.user(
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"ssh-keygen -q -t ed25519 -N '' "
            f"-C {shlex.quote(name)} -f ~/.ssh/id_ed25519 && "
            "cat ~/.ssh/id_ed25519.pub", capture=True).stdout.strip()

        # Pin GitHub host keys from the API (over the host's TLS) instead of
        # trusting ssh-keyscan on first use.
        meta = gh_api("GET", "/meta")
        known = "".join(f"github.com {k}\n" for k in meta["ssh_keys"])

        title = sandbox_sessions.key_title(sandbox_sessions.host_id(),
                                           session_id)
        if self.args.repo:
            log(f"adding deploy key to {self.args.repo}")
            resp = gh_api("POST", f"/repos/{self.args.repo}/keys",
                          {"title": title, "key": pubkey,
                           "read_only": self.args.read_only})
        else:
            log("adding SSH key to your GitHub account")
            resp = gh_api("POST", "/user/keys",
                          {"title": title, "key": pubkey})
        self.key_id = resp["id"]
        return known

    def populate(self, box, session_id):
        if not self.args.repo:
            return HOME
        repo_dir = repo_workdir(self.args.repo)
        log(f"cloning {self.args.repo}")
        box.user(f"git clone git@github.com:{self.args.repo}.git "
                 f"{shlex.quote(repo_dir)}")
        if not self.args.read_only:
            # Never work on the default branch: concurrent sessions each get
            # their own branch under sbx/<session>/ and land through PRs.
            box.user(f"git -C {shlex.quote(repo_dir)} checkout -q -b "
                     + shlex.quote(sandbox_sessions.work_branch(session_id)))
        return repo_dir

    def access_lines(self, workdir, branch):
        return access_lines(workdir, self.args.repo, branch, self.args.read_only)

    def trusted(self):
        """Only for a repo the authenticated user owns. Fails closed."""
        if not self.args.repo:
            return False
        try:
            trusted, note = sandbox_claude_config.trust_decision(
                repo_owner(self.args.repo), self.login)
        except (GitHubError, KeyError, TypeError) as e:
            trusted, note = False, f"couldn't check the repo owner ({e})"
        if note:
            log(f"warning: {note}")
        return trusted

    # -- after the session ---------------------------------------------------------
    def revoke(self):
        if self.key_id is None:
            return
        path = (f"/repos/{self.args.repo}/keys/{self.key_id}"
                if self.args.repo else f"/user/keys/{self.key_id}")
        try:
            gh_api("DELETE", path)
            log(f"removed GitHub key {self.key_id}")
        except Exception as e:  # never skip container cleanup
            log(f"WARNING: could not delete GitHub key {self.key_id}: {e}")
            log("remove it manually at https://github.com/settings/keys"
                if not self.args.repo else
                f"remove it at https://github.com/{self.args.repo}"
                "/settings/keys")

    def land(self, session_id):
        """Open PRs for this session's pushed branches (host-side, with the
        user's gh login). Runs AFTER teardown (the session's key is revoked
        and its container gone), so its branches can't move under the
        checks. Never raises."""
        a = self.args
        if a.repo and not a.read_only and self.key_id is not None:
            land_and_report(a, session_id, open_prs=not a.no_pr)
        return True   # the work is on GitHub; nothing is kept locally
