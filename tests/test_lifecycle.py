"""End-to-end launcher lifecycle with only the I/O edges faked.

Unit tests stub individual Sandbox methods, so a bug in how the pieces fit
together (e.g. an instance attribute shadowing a method that main() calls)
passes them all. These tests run the real main() and real Sandbox methods
against a recording container, a scripted GitHub and a captured subprocess.
"""
import json
import types

import pytest

import claude_sandbox as cs
import sandbox_backend_github as bg
import sandbox_github
import sandbox_land
import sandbox_sessions
from sandbox_github import GitHubError


def test_no_instance_attribute_shadows_a_method(make_args):
    """Regression: `self.session = id` hid the `session()` method, so every
    launch died at the very end with "'str' object is not callable"."""
    box = cs.Sandbox(make_args(repo="o/r"))
    methods = {n for n in dir(cs.Sandbox)
               if callable(getattr(cs.Sandbox, n, None))
               and not n.startswith("__")}
    shadowed = methods & set(vars(box))
    assert not shadowed, f"instance attributes shadow methods: {shadowed}"
    for name in ("session", "land", "clone", "launch", "teardown"):
        assert callable(getattr(box, name))


# A well-formed ed25519 public key: every exec "prints" it, so it serves as
# both the deploy key and the session's signing key.
PUB = "ssh-ed25519 " + __import__("base64").b64encode(
    b"\0\0\0\x0bssh-ed25519\0\0\0\x20" + b"\x07" * 32).decode()


class FakeContainer:
    instances = []

    def __init__(self, cli, name):
        self.cli, self.name, self.calls = cli, name, []
        FakeContainer.instances.append(self)

    @staticmethod
    def list_names(cli):
        return set()

    def _ok(self, what, *a, **k):
        self.calls.append((what, a))
        return types.SimpleNamespace(
            stdout=PUB + "\nsudo=yes\ntools=git\nbranch=\nusb=\n",
            returncode=0)

    def __getattr__(self, what):  # launch, root, put_file, user, delete, ...
        return lambda *a, **k: self._ok(what, *a, **k)

    def user_cmd(self, *argv, env=None):
        return ["fake-exec", "--cwd", cs.HOME, "--", *argv]


class FakeGitHub:
    def __init__(self, session):
        self.calls, self.session = [], session
        self.owner = "studcavity"

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append((method, path, body))
        if "/contents/" in path:
            raise GitHubError("not found", 404)
        if path == "/meta":
            return {"ssh_keys": []}
        if method == "POST" and path.endswith("/keys"):
            return {"id": 5}
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/o/r/pull/7", "number": 7,
                    "head": {"sha": "c" * 40}}
        if path == "/repos/o/r":
            return {"default_branch": "main",
                    "owner": {"login": self.owner}}
        if "/rules/branches/" in path:
            return [{"type": "pull_request"}]
        if path.endswith("/protection"):
            raise GitHubError("not found", 404)
        if path.endswith("/keys?per_page=100"):
            return []
        if "/matching-refs/" in path:
            return [{"ref": f"refs/heads/sbx/{self.session}/work"}]
        if "/compare/" in path:
            return {"ahead_by": 1, "status": "ahead", "total_commits": 1,
                    "files": [{"filename": "a.py"}],
                    "commits": [{"sha": "c" * 40,
                                 "commit": {"message": "do it"}}]}
        if "/pulls?" in path:
            return []
        return None  # DELETE key


@pytest.fixture
def world(monkeypatch, tmp_path):
    FakeContainer.instances = []
    gh = FakeGitHub("quirky-hopper")
    ran = []
    monkeypatch.setattr(cs, "Container", FakeContainer)
    for mod in (bg, sandbox_sessions, sandbox_land, sandbox_github):
        monkeypatch.setattr(mod, "gh_api", gh)
    monkeypatch.setattr(bg.GitHubBackend, "login", "studcavity", raising=False)
    monkeypatch.setattr(bg, "check_workflow_scope", lambda: None)
    monkeypatch.setattr(cs.sandbox_pool, "check", lambda *a: None)
    monkeypatch.setattr(cs, "ensure_base_image", lambda *a, **k: "img")
    monkeypatch.setattr(cs.Sandbox, "attach_volumes", lambda self: None)
    monkeypatch.setattr(cs.signal, "signal", lambda *a: None)
    monkeypatch.setattr(cs.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd))
    return types.SimpleNamespace(gh=gh, ran=ran, tmp=tmp_path)


def argv(world, *extra):
    return ["o/r", "--no-update-check", "--name", "quirky-hopper",
            "--token-file", str(world.tmp / "tok"),
            "--usb-allow", str(world.tmp / "allow"), *extra]


def test_full_launch_runs_session_opens_pr_and_cleans_up(world, capsys):
    assert cs.main(argv(world)) == 0
    # the interactive session actually started, in the clone
    assert any("claude" in c for cmd in world.ran for c in cmd)
    assert any(cs.HOME + "/claude_sandbox" == c or "o/r".split("/")[1] in c
               for cmd in world.ran for c in cmd)
    methods = [(m, p) for m, p, _ in world.gh.calls]
    # key revoked (and container gone) BEFORE the host opens the PR, so the
    # session can't move its branch between the hold check and the PR
    i_pr = methods.index(("POST", "/repos/o/r/pulls"))
    i_key = methods.index(("DELETE", "/repos/o/r/keys/5"))
    assert i_key < i_pr
    box = FakeContainer.instances[-1]
    assert ("delete", ()) in box.calls
    # the work branch was created inside the container
    user_scripts = " ".join(str(c) for what, c in box.calls if what == "user")
    assert "checkout -q -b sbx/quirky-hopper/work" in user_scripts
    # the exit summary was printed
    err = capsys.readouterr().err
    assert "pull requests for session quirky-hopper" in err and "#7" in err


def test_container_is_destroyed_before_any_landing_call(world, monkeypatch):
    """A process the session left running could push between the hold check
    and the PR; so nothing of the session may be alive when landing starts."""
    seen = []
    real_land = cs.Sandbox.land

    def land(self):
        box = FakeContainer.instances[-1]
        seen.append(("delete", ()) in box.calls)
        keys = [(m, p) for m, p, _ in world.gh.calls if m == "DELETE"]
        seen.append(("DELETE", "/repos/o/r/keys/5") in keys)
        return real_land(self)
    monkeypatch.setattr(cs.Sandbox, "land", land)
    assert cs.main(argv(world)) == 0
    assert seen == [True, True]


def test_the_sessions_signing_key_is_recorded_and_its_prs_get_provenance(world, monkeypatch):
    """The public signing key is kept on the host at launch; after teardown
    (the private half is gone) every PR the landing opened is checked."""
    import sandbox_provenance
    posted = []

    def post_session(repo, landed, **kw):
        box = FakeContainer.instances[-1]
        posted.append((repo, [l.outcome for l in landed],
                       ("delete", ()) in box.calls))
        return [sandbox_provenance.Outcome(7, "success", "1 commit signed")]
    monkeypatch.setattr(sandbox_provenance, "post_session", post_session)
    assert cs.main(argv(world)) == 0
    assert sandbox_provenance.load_key("o/r", "quirky-hopper") == PUB
    assert posted == [("o/r", ["opened"], True)]   # after the container is gone
    user_scripts = " ".join(str(c) for what, c in FakeContainer.instances[-1].calls
                            if what == "user")
    assert "commit.gpgsign true" in user_scripts


def test_no_provenance_flag_skips_the_status(world, monkeypatch):
    import sandbox_provenance
    called = []
    monkeypatch.setattr(sandbox_provenance, "post_session",
                        lambda *a, **k: called.append(1) or [])
    assert cs.main(argv(world, "--no-provenance")) == 0
    assert called == []


def test_a_new_session_never_reuses_a_name_with_a_recorded_key(make_args, monkeypatch):
    """Its key would otherwise vouch for the old session's branches."""
    import sandbox_provenance
    sandbox_provenance.save_key("o/r", "quirky-hopper", PUB)
    seen = []
    monkeypatch.setattr(cs.sandbox_sessions, "pick_name",
                        lambda taken: seen.append(set(taken)) or "fresh-name")
    assert cs.Sandbox(make_args(repo="o/r", name=None)).name == "fresh-name"
    assert "quirky-hopper" in seen[0]


def test_landing_still_runs_when_teardown_fails(world, monkeypatch):
    def broken_delete(self):
        raise RuntimeError("incus delete failed")
    monkeypatch.setattr(cs.Sandbox, "teardown", broken_delete)
    with pytest.raises(RuntimeError):
        cs.main(argv(world))
    methods = [(m, p) for m, p, _ in world.gh.calls]
    assert ("POST", "/repos/o/r/pulls") in methods


def test_launcher_waits_for_volume_delete_even_if_landing_is_interrupted(
        world, monkeypatch):
    """Volume deletion runs beside landing; Ctrl-C during landing must still
    wait for it rather than exit with the cleanup half done."""
    waited = []
    monkeypatch.setattr(cs.Sandbox, "wait_volumes",
                        lambda self: waited.append(True))
    monkeypatch.setattr(cs.Sandbox, "land",
                        lambda self: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        cs.main(argv(world))
    assert waited == [True]


def test_no_pr_flag_skips_landing_but_still_cleans_up(world):
    assert cs.main(argv(world, "--no-pr")) == 0
    methods = [(m, p) for m, p, _ in world.gh.calls]
    assert ("POST", "/repos/o/r/pulls") not in methods
    assert ("DELETE", "/repos/o/r/keys/5") in methods


def test_crash_after_launch_still_lands_and_tears_down(world, monkeypatch):
    def boom(self, workdir):
        raise RuntimeError("session blew up")
    monkeypatch.setattr(cs.Sandbox, "session", boom)
    assert cs.main(argv(world)) == 1
    methods = [(m, p) for m, p, _ in world.gh.calls]
    assert ("DELETE", "/repos/o/r/keys/5") in methods
    assert ("delete", ()) in FakeContainer.instances[-1].calls


def test_a_provenance_failure_never_breaks_landing_or_teardown(world, monkeypatch):
    import sandbox_provenance
    monkeypatch.setattr(sandbox_provenance, "post_session",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cs.main(argv(world)) == 0
    assert ("DELETE", "/repos/o/r/keys/5") in [(m, p) for m, p, _ in world.gh.calls]


# --- --timings (plan 1a) ------------------------------------------------------- #
def test_timings_report_names_the_stages_and_the_prompt(world, capsys):
    assert cs.main(argv(world, "--timings")) == 0
    err = capsys.readouterr().err
    for stage in ("host checks (login, scopes)", "container launch + boot", "clone",
                  "session (interactive)", "land: PRs, provenance", "teardown"):
        assert stage in err
    assert "the session prompt appeared after" in err


def test_timings_are_off_by_default_and_leave_no_global_behind(world, capsys):
    import sandbox_timing
    assert cs.main(argv(world)) == 0
    assert "timings" not in capsys.readouterr().err
    assert sandbox_timing.ACTIVE is None
    cs.main(argv(world, "--timings"))
    assert sandbox_timing.ACTIVE is None            # uninstalled even after a run


def test_timings_json_is_written_and_implies_the_report(world, capsys):
    import json
    path = world.tmp / "t.json"
    assert cs.main(argv(world, "--timings-json", str(path))) == 0
    data = json.loads(path.read_text())
    assert data["marks"]["prompt"] >= 0
    assert "teardown" in [s["name"] for s in data["stages"]]
    assert "timings" in capsys.readouterr().err


def test_the_report_survives_a_crash(world, monkeypatch, capsys):
    def boom(self, workdir):
        raise RuntimeError("session blew up")
    monkeypatch.setattr(cs.Sandbox, "session", boom)
    assert cs.main(argv(world, "--timings")) == 1
    assert "teardown" in capsys.readouterr().err


def test_an_unwritable_json_path_is_a_warning_not_a_failure(world, capsys):
    assert cs.main(argv(world, "--timings-json", "/nonexistent-dir/t.json")) == 0
    assert "couldn't write" in capsys.readouterr().err


# --- prefetch wiring (plan 1b) -------------------------------------------------- #
def test_launch_starts_the_prefetch_and_always_clears_it(world, monkeypatch):
    import sandbox_github
    seen = []
    monkeypatch.setattr(sandbox_github, "start_prefetch", lambda specs: seen.append(list(specs)))
    cleared = []
    monkeypatch.setattr(sandbox_github, "clear_prefetch", lambda: cleared.append(1))
    assert cs.main(argv(world)) == 0
    assert seen and ("login",) in seen[0] and cleared == [1]


def test_no_prefetch_flag_starts_nothing_but_still_clears(world, monkeypatch):
    import sandbox_github
    seen, cleared = [], []
    monkeypatch.setattr(sandbox_github, "start_prefetch", lambda specs: seen.append(list(specs)))
    monkeypatch.setattr(sandbox_github, "clear_prefetch", lambda: cleared.append(1))
    assert cs.main(argv(world, "--no-prefetch")) == 0
    assert seen == [[]] and cleared == [1]


# --- stale-while-revalidate wiring (plan 1c) ------------------------------------ #
def test_a_stale_image_launches_now_and_spawns_the_refresh_after_the_launch(world, monkeypatch):
    order = []
    monkeypatch.setattr(cs, "ensure_base_image",
                        lambda *a, on_stale=None, **k: (on_stale("base", "x"), "img")[1])
    monkeypatch.setattr(cs, "spawn_refresh", lambda args: order.append("refresh"))
    orig = FakeContainer._ok

    def spy(self, what, *a, **k):
        if what == "launch":
            order.append("launch")
        return orig(self, what, *a, **k)
    monkeypatch.setattr(FakeContainer, "_ok", spy)
    assert cs.main(argv(world)) == 0
    assert order == ["launch", "refresh"]       # never competes with the launch it follows


def test_nothing_is_spawned_when_the_image_is_fresh(world, monkeypatch):
    monkeypatch.setattr(cs, "spawn_refresh", lambda args: pytest.fail("spawned"))
    assert cs.main(argv(world)) == 0


def test_hard_max_age_zero_turns_the_feature_off(world, monkeypatch):
    seen = {}
    monkeypatch.setattr(cs, "ensure_base_image",
                        lambda *a, **k: seen.update(k) or "img")
    assert cs.main(argv(world, "--hard-max-age", "0")) == 0
    assert seen["hard_max_age_days"] is None


def test_the_default_hard_cap_is_thirty_days(world, monkeypatch):
    seen = {}
    monkeypatch.setattr(cs, "ensure_base_image",
                        lambda *a, **k: seen.update(k) or "img")
    cs.main(argv(world))
    assert seen["hard_max_age_days"] == 30.0 and callable(seen["on_stale"])


def test_own_repo_is_pre_trusted_and_themed(world, monkeypatch):
    sent = []
    monkeypatch.setattr(FakeContainer, "user_argv",
                        lambda self, argv, **k: sent.append((argv, k["input"]))
                        or types.SimpleNamespace(returncode=0, stderr=""),
                        raising=False)
    assert cs.main(argv(world)) == 0
    (argv_, stdin), = sent
    assert argv_[:2] == ["python3", "-c"]
    state = json.loads(stdin)
    assert state[".claude/settings.json"] == {"theme": "dark-daltonized"}
    projects = state[".claude.json"]["projects"]
    assert [v for v in projects.values()] == [{"hasTrustDialogAccepted": True}]


def test_foreign_repo_is_not_pre_trusted(world, monkeypatch, capsys):
    world.gh.owner = "someone-else"
    sent = []
    monkeypatch.setattr(FakeContainer, "user_argv",
                        lambda self, argv, **k: sent.append(k["input"])
                        or types.SimpleNamespace(returncode=0, stderr=""),
                        raising=False)
    assert cs.main(argv(world)) == 0
    assert "projects" not in json.loads(sent[0])[".claude.json"]
    assert "not pre-trusting" in capsys.readouterr().err


def test_owner_lookup_failure_fails_closed(world, monkeypatch):
    sent = []
    monkeypatch.setattr(FakeContainer, "user_argv",
                        lambda self, argv, **k: sent.append(k["input"])
                        or types.SimpleNamespace(returncode=0, stderr=""),
                        raising=False)
    monkeypatch.setattr(bg, "repo_owner",
                        lambda repo: (_ for _ in ()).throw(GitHubError("x", 500)))
    assert cs.main(argv(world)) == 0
    assert "projects" not in json.loads(sent[0])[".claude.json"]


def test_failed_final_check_resumes_the_same_session_before_teardown(
        world, monkeypatch):
    """Exiting Claude runs `check:` in the live container; a failure reopens
    the conversation with the output, and only a pass lets teardown happen."""
    check = {"name": "tests", "path": "final-check.sh", "args": [], "env": {},
             "timeout": 60}

    def load_deps(self):
        self.deps = {"image": None, "apt": [], "scripts": [], "binaries": [],
                     "setup": [], "check": [check]}
    monkeypatch.setattr(cs.Sandbox, "load_deps", load_deps)
    monkeypatch.setattr(cs.Sandbox, "install_deps", lambda self: None)
    events, outcomes = [], [(1, "FAILED tests/test_x.py::test_y"), (0, "ok")]
    monkeypatch.setattr(cs.subprocess, "run",
                        lambda cmd, **k: events.append(("claude", cmd)))

    def stream(cmd, **kw):
        code, out = outcomes.pop(0)
        events.append(("check", code))
        return code, out
    monkeypatch.setattr(cs, "run_streaming", stream)
    monkeypatch.setattr(cs, "ask_resume", lambda names: True)
    monkeypatch.setattr(FakeContainer, "delete",
                        lambda self, *a, **k: events.append(("delete", 0)),
                        raising=False)

    assert cs.main(argv(world)) == 0

    kinds = [k for k, _ in events]
    assert kinds == ["claude", "check", "claude", "check", "delete"]
    first, second = events[0][1], events[2][1]
    assert "--continue" not in " ".join(first)
    assert "--continue" in " ".join(second)
    assert "FAILED tests/test_x.py::test_y" in " ".join(second)


def test_no_final_check_flag_goes_straight_to_teardown(world, monkeypatch):
    monkeypatch.setattr(cs.Sandbox, "load_deps", lambda self: setattr(
        self, "deps", {"image": None, "apt": [], "scripts": [], "binaries": [],
                       "setup": [], "check": [{"name": "t", "path": "c.sh",
                                               "args": [], "env": {},
                                               "timeout": 5}]}))
    monkeypatch.setattr(cs.Sandbox, "install_deps", lambda self: None)
    monkeypatch.setattr(cs, "run_streaming",
                        lambda *a, **k: pytest.fail("ran the check"))
    assert cs.main(argv(world, "--no-final-check")) == 0
