"""sandbox_hardening: the Actions settings check, offers, and where the
findings go. GitHub is faked; the gh wire format is in tests/integration."""
import os

import pytest

import claude_sandbox as cs
import sandbox_backend_github as bg
import sandbox_hardening as sh
from sandbox_github import GitHubError

WF = "/repos/o/r/actions/permissions/workflow"


class Gh:
    """A scripted GitHub: settings in `state`, PUTs recorded and applied."""

    def __init__(self, token="write", approve=True, repo_secrets=0,
                 org_secrets=0, refuse=None, errors=None):
        self.state = {"default_workflow_permissions": token,
                      "can_approve_pull_request_reviews": approve}
        self.secrets, self.org = repo_secrets, org_secrets
        self.refuse = refuse          # code to answer every PUT with
        self.errors = dict(errors or {})   # path-substring -> code for GETs
        self.puts = []

    def __call__(self, method, path, body=None, raw=False):
        if method == "PUT":
            self.puts.append((path, body))
            if self.refuse:
                raise GitHubError("refused", self.refuse)
            self.state.update(body)
            return None
        for frag, code in self.errors.items():
            if frag in path:
                raise GitHubError("nope", code)
        if path == WF:
            return dict(self.state)
        if path.endswith("/actions/secrets?per_page=1"):
            return {"total_count": self.secrets}
        if path.endswith("/organization-secrets?per_page=1"):
            return {"total_count": self.org}
        raise AssertionError(path)


def by_key(found):
    return {c.key: c for c in found}


def test_a_repo_that_meets_the_principle_has_nothing_to_offer():
    found, notes = sh.harden("o/r", Gh("read", False), ask=lambda c: pytest.fail("asked"))
    assert {c.status for c in found} == {sh.MET} and notes == []
    assert sh.warnings(found) == []


def test_unmet_settings_come_with_the_exact_put_that_would_fix_them():
    c = by_key(sh.conditions("o/r", sh.read_settings("o/r", Gh())))
    assert c["token-read-only"].status == sh.UNMET
    assert c["token-read-only"].fix == ("PUT", WF, {"default_workflow_permissions": "read"})
    assert c["actions-cannot-approve"].fix == (
        "PUT", WF, {"can_approve_pull_request_reviews": False})
    assert c["no-reachable-secrets"].status == sh.MET
    assert "PUT" in sh.describe_fix(c["token-read-only"].fix) and WF in sh.describe_fix(
        c["token-read-only"].fix)


def test_each_change_is_applied_only_after_its_own_yes():
    gh, asked = Gh(), []
    found, notes = sh.harden("o/r", gh, ask=lambda c: asked.append(c.key) or c.key == "token-read-only")
    assert asked == ["token-read-only", "actions-cannot-approve"]
    assert gh.puts == [(WF, {"default_workflow_permissions": "read"})]
    assert by_key(found)["token-read-only"].status == sh.MET      # re-read after
    assert by_key(found)["actions-cannot-approve"].status == sh.UNMET
    assert notes == ["token-read-only: applied"]


def test_without_a_prompt_nothing_is_changed_and_it_is_only_reported():
    gh = Gh()
    found, notes = sh.harden("o/r", gh)
    assert gh.puts == [] and notes == []
    w = sh.warnings(found)
    assert len(w) == 2 and all("launch on a terminal" in x for x in w)
    assert all("launch on a terminal" not in x for x in sh.warnings(found, offered=True))


def test_rerunning_after_applying_offers_nothing():
    gh = Gh()
    sh.harden("o/r", gh, ask=lambda c: True)
    assert len(gh.puts) == 2
    found, notes = sh.harden("o/r", gh, ask=lambda c: pytest.fail("asked again"))
    assert {c.status for c in found} == {sh.MET} and gh.puts and len(gh.puts) == 2


@pytest.mark.parametrize("code,why", [(409, "organisation policy"), (403, "admin")])
def test_a_refused_change_is_reported_and_never_fatal(code, why):
    gh = Gh(refuse=code)
    found, notes = sh.harden("o/r", gh, ask=lambda c: True)
    assert all(why in n for n in notes) and len(notes) == 2
    assert by_key(found)["token-read-only"].status == sh.UNMET     # honest afterwards


def test_what_cannot_be_read_is_unknown_with_the_reason():
    gh = Gh(errors={"permissions/workflow": 403, "/actions/secrets": 403})
    found = sh.conditions("o/r", sh.read_settings("o/r", gh))
    c = by_key(found)
    assert {c[k].status for k in c} == {sh.UNKNOWN}
    assert "needs repo admin rights" in c["token-read-only"].text
    assert "repo Actions secrets" in c["no-reachable-secrets"].text


def test_secrets_are_detected_not_fixed_and_only_counted():
    found = sh.conditions("o/r", sh.read_settings("o/r", Gh("read", False, 2, 1)))
    s = by_key(found)["no-reachable-secrets"]
    assert s.status == sh.UNMET and s.fix is None
    assert "2 repo-level and 1 organisation-level" in s.text
    (w,) = sh.warnings(found)
    assert "Environment restricted to the default branch" in w


def test_a_personal_repo_has_no_organisation_secrets_endpoint():
    gh = Gh("read", False, errors={"organization-secrets": 404})
    assert by_key(sh.conditions("o/r", sh.read_settings("o/r", gh)))[
        "no-reachable-secrets"].status == sh.MET


# --- the launcher ----------------------------------------------------------- #
@pytest.fixture
def box(make_args, monkeypatch):
    b = cs.Sandbox(make_args(repo="o/r"))
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: True)
    return b


def use(monkeypatch, gh, answers=()):
    answers = list(answers)
    monkeypatch.setattr(bg, "gh_api", gh)
    monkeypatch.setattr(bg, "ask_hardening", lambda repo, c: answers.pop(0))


def test_a_yes_applies_and_the_launch_log_states_each_condition(box, monkeypatch, capsys):
    gh = Gh()
    use(monkeypatch, gh, [True, True])
    box.backend.harden_actions()
    assert [b for _, b in gh.puts] == [{"default_workflow_permissions": "read"},
                                       {"can_approve_pull_request_reviews": False}]
    err = capsys.readouterr().err
    assert "Actions settings" in err and err.count("ok     ") == 3
    assert box.backend.host_warnings == []


def test_a_no_leaves_a_warning_for_launch_and_exit(box, monkeypatch, capsys):
    use(monkeypatch, Gh(), [False, False])
    box.backend.harden_actions()
    assert "NOT MET" in capsys.readouterr().err
    assert len(box.backend.host_warnings) == 2
    box.report_host_warnings()
    assert "the default workflow token can write" in capsys.readouterr().err


@pytest.mark.parametrize("how", ["flag", "no tty"])
def test_never_prompts_without_a_terminal_or_with_the_flag(box, monkeypatch, how):
    gh = Gh()
    use(monkeypatch, gh, [])          # an unexpected ask would pop an empty list
    if how == "flag":
        box.args.no_hardening_prompt = True
    else:
        monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    box.backend.harden_actions()
    assert gh.puts == [] and len(box.backend.host_warnings) == 2


def test_a_github_failure_never_blocks_the_launch(box, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("gh vanished")
    monkeypatch.setattr(bg, "gh_api", boom)
    box.backend.harden_actions()
    assert "couldn't check the repo's Actions settings" in capsys.readouterr().err


def test_the_preflight_runs_the_check(box, monkeypatch):
    seen = []
    monkeypatch.setattr(box.backend, "protect_default_branch", lambda: seen.append("protect"))
    monkeypatch.setattr(box.backend, "harden_actions", lambda: seen.append("harden"))
    monkeypatch.setattr(cs.Container, "list_names", staticmethod(lambda cli: set()))
    monkeypatch.setattr(cs.sandbox_sessions, "reap_stale_keys", lambda *a: [])
    box.backend.preflight(lambda: cs.Container.list_names("incus"), box.name)
    assert seen == ["protect", "harden"]


def test_nothing_the_check_found_reaches_the_sessions_claude_md(box, repo_root, monkeypatch):
    """A list of the repo's weak points is a map for a prompt-injected session:
    the rendered CLAUDE.md is the same with a clean repo and a bad one."""
    t = open(os.path.join(repo_root, "sandbox_CLAUDE.md")).read()

    def render():
        return cs.render_claude_md(
            t, {"sudo": True, "tools": [], "branch": "", "usb": set()},
            "quirky-hopper", "/h/r", "o/r", False, "", session="quirky-hopper")
    use(monkeypatch, Gh("read", False), [])
    box.backend.harden_actions()
    clean = render()
    box.backend.host_warnings.clear()
    use(monkeypatch, Gh("write", True, 3, 1), [False, False])
    box.backend.harden_actions()
    assert box.backend.host_warnings and render() == clean
    for word in ("secrets exist", "can write to the repo", "default workflow token"):
        assert word not in clean
