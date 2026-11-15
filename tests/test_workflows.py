"""The commit-metadata workflow runs the checker safely."""
import os

import yaml
import pytest

PATH = ".github/workflows/commit-metadata.yml"


@pytest.fixture
def wf(repo_root):
    return yaml.safe_load(open(os.path.join(repo_root, PATH)))


def test_workflow_runs_the_checker_on_prs_with_full_history(wf):
    job = wf["jobs"]["commit-metadata"]
    assert "pull_request" in wf[True]  # YAML parses the key `on` as True
    assert any(s.get("with", {}).get("fetch-depth") == 0
               for s in job["steps"] if "uses" in s)
    run = [s for s in job["steps"] if "sandbox_check.py" in s.get("run", "")]
    assert len(run) == 1


def test_workflow_never_interpolates_untrusted_context_into_scripts(wf):
    for s in wf["jobs"]["commit-metadata"]["steps"]:
        assert "${{" not in s.get("run", "")
    assert wf["permissions"] == {"contents": "read"}
