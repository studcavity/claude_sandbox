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


# --- file-issues: files follow-ups after merge, with the one write it needs --- #
FILER = ".github/workflows/file-issues.yml"


@pytest.fixture
def filer(repo_root):
    return yaml.safe_load(open(os.path.join(repo_root, FILER)))


def test_the_filer_runs_only_on_pushes_to_the_default_branch(filer):
    assert filer[True] == {"push": {"branches": ["main"]}}   # no pull_request


def test_the_filer_has_issues_write_and_nothing_else_beyond_read(filer):
    assert filer["permissions"] == {"contents": "read", "issues": "write"}
    for job in filer["jobs"].values():
        assert "permissions" not in job and "environment" not in job


def test_the_filer_runs_the_checked_out_default_branch_and_no_context_text(filer):
    (job,) = filer["jobs"].values()
    uses = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert len(uses) == 1 and "ref" not in uses[0].get("with", {})
    for s in job["steps"]:
        assert "${{" not in s.get("run", "")      # context goes through env
    run = [s["run"] for s in job["steps"] if "sandbox_issues.py" in s.get("run", "")]
    assert run and "--since" in run[0] and "--last" in run[0]


def test_the_filer_never_overlaps_itself_or_cancels_a_run(filer):
    assert filer["concurrency"]["cancel-in-progress"] is False
    assert filer["defaults"]["run"]["shell"] == "bash -euo pipefail {0}"
