"""End-to-end launcher lifecycle with only the I/O edges faked.

Unit tests stub individual Sandbox methods, so a bug in how the pieces fit
together (e.g. an instance attribute shadowing a method that main() calls)
passes them all. These tests run the real main() and real Sandbox methods
against a recording container, a scripted GitHub and a captured subprocess.
"""
import types

import pytest

import claude_sandbox as cs
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
            stdout="ssh-ed25519 AAAA\nsudo=yes\ntools=git\nbranch=\nusb=\n",
            returncode=0)

    def __getattr__(self, what):  # launch, root, put_file, user, delete, ...
        return lambda *a, **k: self._ok(what, *a, **k)

    def user_cmd(self, *argv, env=None):
        return ["fake-exec", "--cwd", cs.HOME, "--", *argv]


class FakeGitHub:
    def __init__(self, session):
        self.calls, self.session = [], session

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append((method, path, body))
        if "/contents/" in path:
            raise GitHubError("not found", 404)
        if path == "/meta":
            return {"ssh_keys": []}
        if method == "POST" and path.endswith("/keys"):
            return {"id": 5}
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/o/r/pull/7"}
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if "/rules/branches/" in path:
            return [{"type": "pull_request"}]
        if path.endswith("/protection"):
            raise GitHubError("not found", 404)
        if path.endswith("/keys?per_page=100"):
            return []
        if "/matching-refs/" in path:
            return [{"ref": f"refs/heads/sbx/{self.session}/work"}]
        if "/compare/" in path:
            return {"ahead_by": 1, "status": "ahead",
                    "files": [{"filename": "a.py"}],
                    "commits": [{"commit": {"message": "do it"}}]}
        if "/pulls?" in path:
            return []
        return None  # DELETE key


@pytest.fixture
def world(monkeypatch, tmp_path):
    FakeContainer.instances = []
    gh = FakeGitHub("quirky-hopper")
    ran = []
    monkeypatch.setattr(cs, "Container", FakeContainer)
    for mod in (cs, sandbox_sessions, sandbox_land):
        monkeypatch.setattr(mod, "gh_api", gh)
    monkeypatch.setattr(cs.Sandbox, "login", "studcavity", raising=False)
    monkeypatch.setattr(cs, "check_workflow_scope", lambda: None)
    monkeypatch.setattr(cs.sandbox_pool, "check", lambda *a: None)
    monkeypatch.setattr(cs, "ensure_base_image", lambda *a: "img")
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
    # PR opened by the host, then the key revoked, then the container gone
    i_pr = methods.index(("POST", "/repos/o/r/pulls"))
    i_key = methods.index(("DELETE", "/repos/o/r/keys/5"))
    assert i_pr < i_key
    box = FakeContainer.instances[-1]
    assert ("delete", ()) in box.calls
    # the work branch was created inside the container
    user_scripts = " ".join(str(c) for what, c in box.calls if what == "user")
    assert "checkout -q -b sbx/quirky-hopper/work" in user_scripts
    # the exit summary was printed
    err = capsys.readouterr().err
    assert "pull requests for session quirky-hopper" in err and "#7" in err


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


def test_statuses_are_posted_for_the_sessions_prs_when_the_repo_requires_them(world, monkeypatch):
    import sandbox_check
    posted = []
    monkeypatch.setattr(sandbox_check, "gate_required", lambda api, repo: True)
    monkeypatch.setattr(sandbox_check, "post_status_run",
                        lambda repo, pr, result, **k: (posted.append((repo, pr)),
                                                       result.update(state="success", description="ok"))[1])
    assert cs.main(argv(world)) == 0
    assert posted == [("o/r", 7)]          # the PR the fake GitHub "opened"


def test_no_check_status_flag_skips_posting(world, monkeypatch):
    import sandbox_check
    monkeypatch.setattr(sandbox_check, "gate_required", lambda api, repo: True)
    monkeypatch.setattr(sandbox_check, "post_status_run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted")))
    assert cs.main(argv(world, "--no-check-status")) == 0


def test_a_status_failure_never_breaks_landing_or_teardown(world, monkeypatch):
    import sandbox_check
    monkeypatch.setattr(sandbox_check, "gate_required",
                        lambda api, repo: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cs.main(argv(world)) == 0
    assert ("DELETE", "/repos/o/r/keys/5") in [(m, p) for m, p, _ in world.gh.calls]


# --- --timings (plan 1a) ------------------------------------------------------- #
def test_timings_report_names_the_stages_and_the_prompt(world, capsys):
    assert cs.main(argv(world, "--timings")) == 0
    err = capsys.readouterr().err
    for stage in ("github login + scopes", "container launch + boot", "clone",
                  "session (interactive)", "land: PRs, statuses, actions", "teardown"):
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
