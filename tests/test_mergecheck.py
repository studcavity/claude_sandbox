"""sandbox_mergecheck, against real git repos (no mocks for git itself)."""
import subprocess

import pytest

from claude_sandbox import mergecheck as mc

S = "quirky-hopper"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": "/usr/bin:/bin"}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, text=True,
                          capture_output=True)


def commit(cwd, name, text, msg="c"):
    (cwd / name).write_text(text)
    sh(cwd, "add", name)
    assert sh(cwd, "commit", "-qm", msg).returncode == 0


@pytest.fixture
def world(tmp_path):
    """upstream (the base) and a clone with origin/HEAD, as the launcher makes."""
    up = tmp_path / "up"
    up.mkdir()
    sh(up, "init", "-q", "-b", "main")
    commit(up, "a.txt", "one\n")
    clone = tmp_path / "clone"
    assert sh(tmp_path, "clone", "-q", str(up), str(clone)).returncode == 0
    return up, clone


def git_in(cwd):
    def git(*args):
        r = sh(cwd, *args)
        return r.returncode, r.stdout
    return git


def branch(clone, name, fname, text, push=True):
    sh(clone, "switch", "-q", "-c", f"sbx/{S}/{name}", "origin/main")
    commit(clone, fname, text)
    if push:
        assert sh(clone, "push", "-q", "origin", "HEAD").returncode == 0
    sh(clone, "switch", "-q", "--detach", "origin/main")


def test_a_branch_ahead_of_an_unmoved_base_is_fine(world):
    _, clone = world
    branch(clone, "ok", "b.txt", "x\n")
    assert mc.inspect(git_in(clone), S, remote=True) == ([], [])


def test_a_branch_behind_the_base_is_found_after_fetching(world):
    up, clone = world
    branch(clone, "old", "b.txt", "x\n")
    commit(up, "c.txt", "later\n")
    findings, _ = mc.inspect(git_in(clone), S, remote=True)
    assert findings == [(f"sbx/{S}/old", "1 commit(s) behind the base")]


def test_a_conflicting_branch_names_the_files(world):
    up, clone = world
    branch(clone, "clash", "a.txt", "mine\n")
    commit(up, "a.txt", "theirs\n")
    (finding,) = [f for f in mc.inspect(git_in(clone), S, remote=True)[0]
                  if "conflicts" in f[1]]
    assert finding == (f"sbx/{S}/clash", "conflicts with the base in: a.txt")


def test_every_session_branch_is_judged_alone(world):
    up, clone = world
    branch(clone, "one", "a.txt", "mine\n")
    branch(clone, "two", "b.txt", "x\n")
    branch(clone, "three", "c.txt", "y\n")
    commit(up, "a.txt", "theirs\n")
    findings, _ = mc.inspect(git_in(clone), S, remote=True)
    assert sorted({b for b, _ in findings}) == [
        f"sbx/{S}/one", f"sbx/{S}/three", f"sbx/{S}/two"]
    assert [p for b, p in findings if b.endswith("/two")] == [
        "1 commit(s) behind the base"]


def test_other_sessions_and_unrelated_branches_are_ignored(world):
    up, clone = world
    sh(clone, "switch", "-q", "-c", "sbx/other-one/x", "origin/main")
    commit(clone, "z.txt", "z\n")
    sh(clone, "push", "-q", "origin", "HEAD")
    sh(clone, "switch", "-q", "-c", "feature", "origin/main")
    commit(clone, "f.txt", "f\n")
    sh(clone, "push", "-q", "origin", "HEAD")
    commit(up, "c.txt", "later\n")
    assert mc.inspect(git_in(clone), S, remote=True)[0] == []


def test_unpushed_and_diverged_local_branches_are_reported(world):
    _, clone = world
    branch(clone, "never", "n.txt", "x\n", push=False)
    branch(clone, "moved", "m.txt", "x\n")
    sh(clone, "switch", "-q", f"sbx/{S}/moved")
    commit(clone, "m2.txt", "more\n")
    findings, _ = mc.inspect(git_in(clone), S, remote=True)
    assert (f"sbx/{S}/never", "never pushed (no PR would be opened)") in findings
    assert any(b.endswith("/moved") and "differs" in p for b, p in findings)


def test_a_fetched_base_that_renamed_its_default_branch_is_followed(world):
    up, clone = world
    sh(up, "branch", "-m", "trunk")
    commit(up, "t.txt", "t\n")
    branch_ok = sh(clone, "switch", "-q", "-c", f"sbx/{S}/x", "origin/main")
    assert branch_ok.returncode == 0
    commit(clone, "x.txt", "x\n")
    sh(clone, "push", "-q", "origin", "HEAD")
    findings, _ = mc.inspect(git_in(clone), S, remote=True)
    assert findings == [(f"sbx/{S}/x", "1 commit(s) behind the base")]


def test_local_mode_reads_local_branches_and_never_fetches(world):
    _, clone = world
    branch(clone, "mine", "b.txt", "x\n", push=False)
    calls = []

    def git(*args):
        calls.append(args[0])
        return git_in(clone)(*args)
    assert mc.inspect(git, S, remote=False) == ([], [])
    assert "fetch" not in calls and "remote" not in calls


def test_an_empty_branch_is_not_a_finding_even_when_behind(world):
    up, clone = world
    sh(clone, "branch", f"sbx/{S}/empty", "origin/main")
    sh(clone, "push", "-q", "origin", f"sbx/{S}/empty")
    commit(up, "c.txt", "later\n")
    assert mc.inspect(git_in(clone), S, remote=True)[0] == []


def test_a_failed_fetch_is_a_note_not_a_finding(world):
    _, clone = world
    real = git_in(clone)

    def git(*args):
        return (1, "") if args[0] == "fetch" else real(*args)
    findings, notes = mc.inspect(git, S, remote=True)
    assert findings == [] and "couldn't fetch" in notes[0]


def test_no_base_is_a_note():
    findings, notes = mc.inspect(lambda *a: (1, ""), S, remote=False)
    assert findings == [] and "can't find the base" in notes[0]


@pytest.mark.parametrize("rc, out, want", [
    (0, "tree\n", []), (1, "tree\na.txt\nb/c.txt\n\nmsg\n", ["a.txt", "b/c.txt"]),
    (1, "tree\n", []), (128, "fatal", None)])
def test_parse_conflicts(rc, out, want):
    assert mc.parse_conflicts(rc, out) == want


def test_resume_text_matches_the_backend():
    f = [("sbx/s/x", "1 commit(s) behind the base")]
    assert "--force-with-lease" in mc.resume_text(f, True)
    local = mc.resume_text(f, False)
    assert "--force-with-lease" not in local and "import" in local
