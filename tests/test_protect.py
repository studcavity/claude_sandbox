"""Tests for protect and the launcher's protection prompt."""
import json
import os

import pytest

from claude_sandbox import cli as cs
from claude_sandbox import render
from claude_sandbox import sandbox
from claude_sandbox.backends import github as bg
from claude_sandbox.github import protect as sp
from claude_sandbox.github.gh import GitHubError
from claude_sandbox import sessions
from claude_sandbox.github import protect


@pytest.fixture
def base(repo_root):
    return sp.load_ruleset(os.path.join(repo_root, "rulesets",
                                        "protect-main.json"))


def test_with_checks_adds_non_strict_required_rule_without_mutating(base):
    n = len(base["rules"])
    rs = sp.with_checks(base, ["test (3.12)", "lint"])
    assert len(base["rules"]) == n  # original untouched
    rule = rs["rules"][-1]
    assert rule["type"] == "required_status_checks"
    assert rule["parameters"]["strict_required_status_checks_policy"] is False
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "lint"}, {"context": "test (3.12)"}]
    assert sp.with_checks(base, []) == base


def test_discover_checks_only_successes_deduped(monkeypatch):
    monkeypatch.setattr(sp, "gh_api", lambda *a, **k: {"check_runs": [
        {"name": "test (3.12)", "conclusion": "success"},
        {"name": "test (3.12)", "conclusion": "success"},
        {"name": "flaky", "conclusion": "failure"},
        {"name": "running", "conclusion": None}]})
    assert sp.discover_checks("o/r", "main") == ["test (3.12)"]


def test_discover_checks_is_best_effort(monkeypatch):
    def boom(*a, **k):
        raise GitHubError("x", 403)
    monkeypatch.setattr(sp, "gh_api", boom)
    assert sp.discover_checks("o/r", "main") == []


def test_apply_posts_the_ruleset(monkeypatch, base):
    seen = []
    monkeypatch.setattr(sp, "gh_api", lambda m, p, body=None, **k:
                        seen.append((m, p, body)) or {"id": 9})
    assert sp.apply_ruleset("o/r", base) == 9
    assert seen == [("POST", "/repos/o/r/rulesets", base)]


# -- launcher flow --------------------------------------------------------- #
@pytest.fixture
def flow(make_args, monkeypatch):
    """Sandbox.protect_default_branch with a scripted GitHub and terminal."""
    st = {"state": "unprotected", "applied": [], "asked": [], "answers": [],
          "existing": set(), "checks": [], "repo_rulesets": ([], []),
          "fetch_args": []}
    box = sandbox.Sandbox(make_args(repo="o/r", no_protect_prompt=False))
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sessions, "check_protection",
                        lambda repo: ("main", "WARN" if st["state"] !=
                                      "protected" else None, st["state"]))
    monkeypatch.setattr(protect, "existing_ruleset_names",
                        lambda r: st["existing"])
    monkeypatch.setattr(protect, "fetch_repo_rulesets",
                        lambda r, b: st["fetch_args"].append((r, b))
                        or st["repo_rulesets"])
    monkeypatch.setattr(protect, "discover_checks",
                        lambda r, b: st["checks"])
    st.setdefault("required", {"ci"})   # satisfied by default: no gap
    monkeypatch.setattr(protect, "required_on",
                        lambda r, b: st["required"])
    def apply(r, rs):
        st["applied"].append(rs)
        st["required"] = set(st.get("required", ())) | set(sp.required_checks(rs))
        return 1
    monkeypatch.setattr(protect, "apply_ruleset", apply)

    def answer(*a):
        st["asked"].append(a)
        return st["answers"].pop(0)
    monkeypatch.setattr(bg, "ask_protect", lambda *a: answer("protect"))
    monkeypatch.setattr(bg, "ask_checks", lambda *a: answer("checks"))
    monkeypatch.setattr(bg, "ask_ruleset", lambda *a: answer(("ruleset", a[1])))
    return box, st


def warned(box):
    return "\n".join(box.backend.host_warnings)


def test_declined_applies_nothing_and_warns_the_user(flow):
    box, st = flow
    st["answers"] = [False]
    box.backend.protect_default_branch()
    assert st["applied"] == []
    assert "does not require pull requests" in warned(box)


def test_yes_applies_ruleset_without_checks_by_default(flow):
    box, st = flow
    st["answers"] = [True]
    box.backend.protect_default_branch()
    (rs,) = st["applied"]
    assert "required_status_checks" not in {r["type"] for r in rs["rules"]}
    assert "does not require" not in warned(box)


def test_yes_to_checks_requires_the_discovered_names(flow):
    box, st = flow
    st["checks"], st["answers"] = ["lint"], [True, True]
    box.backend.protect_default_branch()
    (rs,) = st["applied"]
    assert rs["rules"][-1]["parameters"]["required_status_checks"] == [
        {"context": "lint"}]
    assert "`lint`" not in warned(box)  # required now, so no gap


def test_no_to_checks_still_applies_base_ruleset(flow):
    box, st = flow
    st["checks"], st["answers"] = ["test (3.12)"], [True, False]
    box.backend.protect_default_branch()
    assert "required_status_checks" not in {
        r["type"] for r in st["applied"][0]["rules"]}


def test_never_prompts_when_protected_or_flagged_or_not_a_tty(flow,
                                                              monkeypatch):
    box, st = flow
    st["state"] = "protected"
    box.backend.protect_default_branch()
    assert st["asked"] == [] and box.backend.host_warnings == []

    st["state"] = "unprotected"
    box.args.no_protect_prompt = True
    box.backend.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []

    box.args.no_protect_prompt = False
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    box.backend.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []


def test_unknown_state_is_not_offered_a_change(flow):
    box, st = flow
    st["state"] = "unknown"
    box.backend.protect_default_branch()
    assert st["asked"] == [] and "couldn't verify" in warned(box)


def test_existing_same_named_ruleset_is_not_duplicated(flow):
    box, st = flow
    st["existing"] = {"protect-default-branch"}
    box.backend.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []


def test_apply_failure_never_aborts_and_note_stays_honest(flow, monkeypatch):
    box, st = flow
    st["answers"] = [True]

    def boom(r, rs):
        raise GitHubError("403 plan has no rulesets", 403)
    monkeypatch.setattr(protect, "apply_ruleset", boom)
    box.backend.protect_default_branch()  # must not raise
    assert "does not require pull requests" in warned(box)


# -- gaps are warnings for the user, never text the session reads -------------- #
def test_gap_warnings_wording_and_the_suggested_fix():
    gw = sp.gap_warnings
    assert gw("o/r", "main", "protected", {"lint"}, ["lint"]) == []
    assert gw("o/r", "main", "protected", None, ["lint"]) == []      # can't tell
    (w,) = gw("o/r", "main", "protected", set(), ["lint", "docs"])
    assert "no status check is required" in w and "`docs`, `lint`" in w
    assert "gh api repos/o/r/rulesets" in w
    (w,) = gw("o/r", "main", "applied", {"lint"}, ["lint", "docs"])
    assert "CI jobs not required" in w and "`docs`" in w and "`lint`" not in w
    assert gw("o/r", "main", "protected", set(), []) == [
        "no status check is required to merge into `main` of o/r"]
    (w,) = gw("o/r", "main", "unprotected", None, [])
    assert "does not require pull requests" in w
    (w,) = gw("o/r", "main", "unknown", None, [])
    assert "couldn't verify" in w
    assert gw("o/r", "main", "unprotected", set(), ["ci"]) == [w for w in gw(
        "o/r", "main", "unprotected", None, [])]   # one warning, not two


def test_required_checks_are_read_from_rulesets_and_classic_protection():
    rules = [{"type": "pull_request"},
             {"type": "required_status_checks", "parameters": {
                 "required_status_checks": [{"context": "a"}, {"context": "b"}]}}]
    assert sp.rule_checks(rules) == {"a", "b"} and sp.rule_checks(None) == set()
    assert sp.classic_checks({"required_status_checks": {
        "contexts": ["c"], "checks": [{"context": "d"}]}}) == {"c", "d"}
    assert sp.classic_checks({}) == set()


def test_required_on_merges_both_and_reports_unreadable_as_none(monkeypatch):
    def api(m, p, body=None, **k):
        if "/rules/branches/" in p:
            return [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "a"}]}}]
        raise GitHubError("no classic protection", 404)
    monkeypatch.setattr(sp, "gh_api", api)
    assert sp.required_on("o/r", "main") == {"a"}

    def forbidden(m, p, body=None, **k):
        raise GitHubError("forbidden", 403)
    monkeypatch.setattr(sp, "gh_api", forbidden)
    assert sp.required_on("o/r", "main") is None


def test_exit_summary_repeats_the_launch_warnings(flow, capsys):
    box, st = flow
    st["state"] = "unknown"
    box.backend.protect_default_branch()
    launch = capsys.readouterr().err
    assert "warning: couldn't verify" in launch
    box.report_host_warnings()
    exit_ = capsys.readouterr().err
    assert "couldn't verify" in exit_ and "the session was not told" in exit_
    box.backend.host_warnings.clear()
    box.report_host_warnings()
    assert capsys.readouterr().err == ""


def test_a_protected_repo_with_unrequired_ci_jobs_warns_with_the_names(flow):
    box, st = flow
    st["state"], st["checks"], st["required"] = "protected", ["lint", "test"], {"lint"}
    box.backend.protect_default_branch()
    assert "`test`" in warned(box) and "`lint`" not in warned(box)


STATES = [("protected", set(), []), ("protected", {"lint"}, ["lint"]),
          ("protected", set(), ["lint"]), ("unprotected", set(), []),
          ("unknown", set(), []), ("applied", {"lint"}, ["lint", "test"])]


def _render(repo_root, box):
    t = open(os.path.join(repo_root, "claude_sandbox", "data", "sandbox_CLAUDE.md")).read()
    return render.render_claude_md(
        t, {"sudo": True, "tools": [], "branch": "", "usb": set()},
        "quirky-hopper", "/h/r", "o/r", False, "", session="quirky-hopper")


def test_the_sessions_claude_md_is_the_same_whatever_the_protection_state(
        flow, repo_root, monkeypatch):
    """A list of the repo's weak points is a map for a prompt-injected
    session, so the rendered text must not depend on any of them."""
    box, st = flow
    rendered = set()
    for state, required, ci in STATES:
        st.update(state=state, required=set(required), checks=list(ci),
                  answers=[False, False], asked=[])
        box.backend.host_warnings.clear()
        box.backend.protect_default_branch()
        rendered.add(_render(repo_root, box))
    assert len(rendered) == 1
    (md,) = rendered
    for gap in ("does not require", "does **not** require", "none known",
                "could not be verified", "No status checks", "applied the "):
        assert gap not in md


def test_only_the_matrix_fragility_reaches_the_session(flow, repo_root):
    """The one exception to the test above: matrix-named required checks are
    something the session can fix, so it is told (and nothing else about the
    repo's protection)."""
    box, st = flow
    ci = ["tests-passed", "test (3.12)"]
    st.update(state="protected", required={"test (3.12)"}, checks=ci,
              answers=[False, False], asked=[])
    box.backend.protect_default_branch()
    assert box.backend.matrix_ci is True
    md = render.render_claude_md(
        open(os.path.join(repo_root, "claude_sandbox", "data",
                          "sandbox_CLAUDE.md")).read(),
        {"sudo": True, "tools": [], "branch": "", "usb": set()},
        "quirky-hopper", "/h/r", "o/r", False, "", session="quirky-hopper",
        ci_note=render.MATRIX_CI_NOTE)
    assert "ask the user straight away" in md and "aggregator job" in md
    assert "are required to merge" in md and "tests-passed" not in md
    # fixed (a static gate is required, no matrix name is): not flagged
    st.update(required={"tests-passed"})
    box.backend.protect_default_branch()
    assert box.backend.matrix_ci is False


def test_render_claude_md_takes_no_protection_input():
    import inspect
    assert "protection_note" not in inspect.signature(render.render_claude_md).parameters
    assert not hasattr(cs, "protection_note")


def test_template_covers_branches_and_status_checks(repo_root):
    t = open(os.path.join(repo_root, "claude_sandbox", "data", "sandbox_CLAUDE.md")).read()
    md = render.render_claude_md(
        t, {"sudo": True, "tools": [], "branch": "", "usb": set()},
        "quirky-hopper", "/h/r", "o/r", False, "", session="quirky-hopper")
    assert "stack" not in md.lower()  # spec 05: every PR targets the default
    assert "sbx/quirky-hopper/02-<task>" in md
    assert "never push to it" in md and "strands every open PR" in md
    assert "(`o/r`)" in md
    assert not __import__("re").search(r"\$[a-z_{]", md)


def test_ruleset_roundtrips_as_json(base):
    json.dumps(sp.with_checks(base, ["x"]))


# -- rulesets defined by the repo itself ------------------------------------ #
def repo_rs(**kw):
    rs = {"name": "require-x", "target": "branch", "enforcement": "active",
          "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"],
                                      "exclude": []}},
          "bypass_actors": [],
          "rules": [{"type": "required_status_checks", "parameters": {
              "required_status_checks": [{"context": "ci"}]}}]}
    rs.update(kw)
    return rs


def test_valid_repo_ruleset_is_normalised_and_input_untouched():
    src = repo_rs()
    del src["bypass_actors"]
    out = sp.validate_repo_ruleset(src)
    assert out["bypass_actors"] == [] and "bypass_actors" not in src


@pytest.mark.parametrize("bad", [
    [], "x", None, {**repo_rs(), "extra": 1},
    repo_rs(name=""), repo_rs(name="a/b"), repo_rs(name=5),
    repo_rs(target="tag"), repo_rs(enforcement="disabled"),
    repo_rs(bypass_actors=[{"actor_id": 5, "actor_type": "RepositoryRole"}]),
    repo_rs(conditions={"ref_name": {"include": ["refs/heads/*"],
                                     "exclude": []}}),
    repo_rs(conditions={"ref_name": {"include": ["~DEFAULT_BRANCH"],
                                     "exclude": ["main"]}}),
    repo_rs(conditions={"ref_name": {"include": ["~DEFAULT_BRANCH"],
                                     "exclude": []}, "repository_name": {}}),
    repo_rs(conditions=[]), repo_rs(conditions=None),
    repo_rs(rules=[]), repo_rs(rules=[{"type": "workflows"}]),
    repo_rs(rules=[{"type": "deletion", "x": 1}]),
    repo_rs(rules=[{"type": "deletion", "parameters": []}]),
    repo_rs(rules="deletion"), repo_rs(rules=["deletion"]),
])
def test_unsafe_or_malformed_repo_rulesets_are_rejected(bad):
    with pytest.raises(ValueError):
        sp.validate_repo_ruleset(bad)


class Contents:
    """Fake contents API for rulesets/: {name: (size, text)}."""
    def __init__(self, files, listing_error=None):
        self.files, self.error, self.calls = files, listing_error, []

    def __call__(self, method, path, body=None, raw=False):
        self.calls.append(path)
        if self.error:
            raise self.error
        if path.startswith("/repos/o/r/contents/rulesets?"):
            return [{"type": "file", "name": n, "size": sz}
                    for n, (sz, _) in self.files.items()] + [
                {"type": "dir", "name": "sub.json"}]
        name = path.split("/rulesets/")[1].split("?")[0]
        return self.files[name][1].encode()


def test_fetch_reads_default_branch_and_skips_bad_files(monkeypatch):
    good = json.dumps(repo_rs())
    api = Contents({"ok.json": (len(good), good),
                    "bad.json": (3, "[1]"),
                    "evil.json": (10, json.dumps(repo_rs(bypass_actors=[1]))),
                    "junk.json": (4, "nope"),
                    "big.json": (sp.MAX_RULESET_BYTES + 1, "{}"),
                    "notes.md": (1, "x"), ".hidden.json": (1, "{}")})
    monkeypatch.setattr(sp, "gh_api", api)
    found, problems = sp.fetch_repo_rulesets("o/r", "my branch")
    assert [r["name"] for r in found] == ["require-x"]
    assert len(problems) == 4 and all("rulesets/" in p for p in problems)
    assert all("ref=my%20branch" in c for c in api.calls)
    assert not any("notes.md" in c or "hidden" in c or "big.json" in c
                   for c in api.calls[1:])


def test_fetch_no_directory_is_normal_other_errors_raise(monkeypatch):
    monkeypatch.setattr(sp, "gh_api", Contents({}, GitHubError("nf", 404)))
    assert sp.fetch_repo_rulesets("o/r", "main") == ([], [])
    monkeypatch.setattr(sp, "gh_api", Contents({}, GitHubError("no", 500)))
    with pytest.raises(GitHubError):
        sp.fetch_repo_rulesets("o/r", "main")


def test_fetch_caps_the_number_of_files(monkeypatch):
    t = json.dumps(repo_rs())
    api = Contents({f"r{i}.json": (len(t), t) for i in range(15)})
    monkeypatch.setattr(sp, "gh_api", api)
    found, problems = sp.fetch_repo_rulesets("o/r", "main")
    assert len(found) == sp.MAX_REPO_RULESETS and "15 ruleset files" in problems[0]


def test_pending_required_checks_and_description():
    a, b = repo_rs(), repo_rs(name="other")
    assert sp.pending([a, b], {"require-x"}) == [b]
    assert sp.required_checks(a) == ["ci"]
    text = "\n".join(sp.describe_rules(a, "main"))
    assert "`ci`" in text and "no bypass actors" in text
    both = repo_rs(rules=[{"type": "pull_request"}, {"type": "deletion"}],
                   enforcement="evaluate")
    text = "\n".join(sp.describe_rules(both, "main"))
    assert "pull request" in text and "deletion" in text and "evaluate" in text


def test_repo_defined_rulesets_replace_the_generic_one(flow):
    box, st = flow
    st["repo_rulesets"] = ([repo_rs()], [])
    st["answers"] = [True]
    box.backend.protect_default_branch()
    (rs,) = st["applied"]
    assert rs["name"] == "require-x"          # not protect-default-branch
    assert st["asked"] == [(("ruleset", "require-x"),)]  # one question only
    assert "No status checks" not in warned(box)


def test_repo_ruleset_is_offered_even_on_a_protected_branch(flow):
    box, st = flow
    st["state"] = "protected"
    st["repo_rulesets"] = ([repo_rs()], [])
    st["answers"] = [True]
    box.backend.protect_default_branch()
    assert len(st["applied"]) == 1 and box.backend.host_warnings == []


def test_declined_and_existing_repo_rulesets_apply_nothing(flow):
    box, st = flow
    st["state"] = "protected"
    st["repo_rulesets"] = ([repo_rs()], [])
    st["answers"] = [False]
    box.backend.protect_default_branch()
    assert st["applied"] == [] and len(st["asked"]) == 1
    st["existing"], st["asked"] = {"require-x"}, []
    box.backend.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []


def test_protected_repo_with_no_repo_rulesets_is_never_prompted(flow):
    box, st = flow
    st["state"] = "protected"
    box.backend.protect_default_branch()
    assert st["asked"] == [] and st["applied"] == []
    assert st["fetch_args"] == [("o/r", "main")]


def test_each_repo_ruleset_is_asked_separately(flow):
    box, st = flow
    st["repo_rulesets"] = ([repo_rs(name="one"), repo_rs(name="two")], [])
    st["answers"] = [True, False]
    box.backend.protect_default_branch()
    assert [r["name"] for r in st["applied"]] == ["one"]
    assert len(st["asked"]) == 2


def test_repo_rulesets_are_never_fetched_without_a_prompt(flow, monkeypatch):
    box, st = flow
    box.args.no_protect_prompt = True
    box.backend.protect_default_branch()
    monkeypatch.setattr(cs.sys.stdin, "isatty", lambda: False)
    box.args.no_protect_prompt = False
    box.backend.protect_default_branch()
    assert st["fetch_args"] == []


def test_this_repos_own_rulesets_are_valid_and_their_checks_exist(repo_root):
    """Cross-reference: every ruleset we ship passes the same validation a
    repo-defined one must, and every check it requires is a real CI job here
    (a required check that never reports blocks every merge)."""
    import glob
    import yaml
    jobs = set()
    for wf in glob.glob(os.path.join(repo_root, ".github", "workflows", "*.yml")):
        for key, job in yaml.safe_load(open(wf))["jobs"].items():
            jobs.add(job.get("name", key))
    required = set()
    for path in glob.glob(os.path.join(repo_root, "rulesets", "*.json")):
        rs = sp.validate_repo_ruleset(sp.load_ruleset(path))
        required.update(sp.required_checks(rs))
    from claude_sandbox import provenance as provenance
    assert "commit-metadata" in required
    # sandbox/provenance is a commit STATUS the launcher posts, not a CI job
    statuses = {provenance.CONTEXT}
    assert statuses <= required
    assert required - statuses <= jobs


# --- rulesets/optional: opt-in, applied by hand ---------------------------- #
OPTIONAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "rulesets", "optional")
ADMIN_BYPASS = [{"actor_id": 5, "actor_type": "RepositoryRole",
                 "bypass_mode": "always"}]


def _optional(name):
    with open(os.path.join(OPTIONAL, name)) as f:
        return json.load(f)


def test_branch_ruleset_confines_non_admins_to_sandbox_branches():
    """Deploy keys aren't admins, so with this a session can only create,
    update or delete branches under sbx/ (no gh-pages, no release branch)."""
    rs = _optional("sandbox-branches-only.json")
    assert rs["target"] == "branch" and rs["enforcement"] == "active"
    assert rs["conditions"]["ref_name"] == {"include": ["~ALL"],
                                            "exclude": ["refs/heads/sbx/**"]}
    assert rs["bypass_actors"] == ADMIN_BYPASS   # never DeployKey
    assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion"}


def test_tag_ruleset_stops_non_admins_pushing_tags():
    """A pushed tag runs tag-triggered (release) workflows on code the
    session wrote, with whatever publishing secrets they use."""
    rs = _optional("no-tag-pushes.json")
    assert rs["target"] == "tag" and rs["bypass_actors"] == ADMIN_BYPASS
    assert rs["conditions"]["ref_name"]["include"] == ["~ALL"]
    assert {r["type"] for r in rs["rules"]} == {"creation", "update", "deletion"}


def test_optional_rulesets_are_never_offered_automatically():
    """They carry a bypass actor, which repo-supplied rulesets must not: the
    launcher's validator refuses them, and they live in a subdirectory the
    launcher's listing skips."""
    for name in os.listdir(OPTIONAL):
        with pytest.raises(ValueError):
            sp.validate_repo_ruleset(_optional(name))


def test_required_actions_checks_are_pinned_to_github_actions():
    """Without integration_id, any app or token that can post a status named
    `commit-metadata` satisfies the requirement."""
    root = os.path.dirname(OPTIONAL)
    with open(os.path.join(root, "require-commit-metadata.json")) as f:
        rs = json.load(f)
    (rule,) = rs["rules"]
    assert rule["parameters"]["required_status_checks"] == [
        {"context": "commit-metadata", "integration_id": 15368}]


def test_linear_history_is_not_something_the_launcher_initialises(repo_root):
    base = sp.load_ruleset(os.path.join(repo_root, "rulesets",
                                        "protect-main.json"))
    assert "required_linear_history" not in {r["type"] for r in base["rules"]}
    assert "linear" not in " ".join(sp.describe(base, "main"))
    with pytest.raises(ValueError):
        sp.validate_repo_ruleset(
            {**base, "rules": [{"type": "required_linear_history"}]})


# -- matrix entries are never offered; an aggregator is advised ---------------- #
def test_matrix_names_are_told_apart_from_static_ones():
    assert sp.split_matrix(["lint", "test (3.12)", "test (3.14)",
                            "caller / build", "sandbox/provenance"]) == (
        ["caller / build", "lint", "sandbox/provenance"],
        ["test (3.12)", "test (3.14)"])


def test_offerable_checks_drops_matrix_entries(monkeypatch):
    monkeypatch.setattr(sp, "gh_api", lambda *a, **k: {"check_runs": [
        {"name": "lint", "conclusion": "success"},
        {"name": "test (3.12)", "conclusion": "success"}]})
    assert sp.offerable_checks("o/r", "main") == ["lint"]


def test_matrix_warning_when_a_matrix_name_is_required_or_nothing_gates_it():
    gw = sp.gap_warnings
    ci = ["tests-passed", "test (3.12)", "test (3.14)"]
    # hard-coded matrix names: fragile
    ws = gw("o/r", "main", "protected", {"test (3.12)", "tests-passed"}, ci)
    (w,) = [w for w in ws if "matrix" in w]
    assert "`test (3.12)`" in w and "aggregator" in w and "if: always()" in w
    # nothing static required: told too, and never offered the matrix names
    ws = gw("o/r", "main", "protected", set(), ci)
    assert any("matrix builds: `test (3.12)`, `test (3.14)`" in w for w in ws)
    assert "`test (3.12)`, " not in ws[0]   # the 'no check required' list is static only
    # a static (aggregator) check is required and no matrix name is: quiet
    assert gw("o/r", "main", "protected", {"tests-passed"}, ci) == []
    # unknown requirements: quiet
    assert gw("o/r", "main", "protected", None, ci) == []


def test_launcher_offers_only_static_checks(flow):
    box, st = flow
    st["checks"] = ["lint", "test (3.12)"]
    st["answers"] = [True, True]
    box.backend.protect_default_branch()
    names = [c["context"] for c in
             st["applied"][0]["rules"][-1]["parameters"]["required_status_checks"]]
    assert names == ["lint"]
