"""The commit-metadata workflow runs the checker safely."""
import os

import yaml
import pytest

PATH = ".github/workflows/commit-metadata.yml"


@pytest.fixture
def wf(repo_root):
    return yaml.safe_load(open(os.path.join(repo_root, PATH)))


def _checkouts(wf):
    return [s for s in wf["jobs"]["commit-metadata"]["steps"]
            if str(s.get("uses", "")).startswith("actions/checkout")]


def test_workflow_runs_on_prs_and_checks_the_prs_full_history(wf):
    assert "pull_request" in wf[True]  # YAML parses the key `on` as True
    by_path = {c["with"]["path"]: c["with"] for c in _checkouts(wf)}
    assert by_path["pr"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert by_path["pr"]["fetch-depth"] == 0


def test_checker_code_comes_from_the_base_not_the_pr(wf):
    """A PR must not be able to weaken the rules it is judged by."""
    by_path = {c["with"]["path"]: c["with"] for c in _checkouts(wf)}
    assert by_path["trusted"]["ref"] == (
        "${{ github.event.pull_request.base.sha }}")
    (run,) = [s["run"] for s in wf["jobs"]["commit-metadata"]["steps"]
              if "sandbox_check.py" in s.get("run", "")]
    # always the trusted checkout: no fallback to the PR's own copy
    assert "../trusted/sandbox_check.py" in run
    assert "pr/sandbox_check.py" not in run and "$src" not in run
    # stdlib-only checker: nothing is installed (no PR-chosen packages)
    assert "pip install" not in run


def test_fork_prs_are_checked_not_skipped(wf):
    """A job skipped by a job-level `if` counts as passing for a required
    check, so a fork PR would sail through the gate unchecked."""
    assert "if" not in wf["jobs"]["commit-metadata"]


def test_workflow_never_interpolates_untrusted_context_into_scripts(wf):
    for s in wf["jobs"]["commit-metadata"]["steps"]:
        assert "${{" not in s.get("run", "")
    assert wf["permissions"] == {"contents": "read"}
