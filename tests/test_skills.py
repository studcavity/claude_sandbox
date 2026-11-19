"""skills.py: the submodule refresh decision, skill discovery, and what is
shipped into the container. No network: invoke's Context is faked."""
import io
import os
import tarfile
import types

import pytest
from invoke.exceptions import CommandTimedOut

from claude_sandbox import cli
from claude_sandbox import sandbox
from claude_sandbox import skills


def make_skill(base, rel, files=None):
    d = os.path.join(base, rel)
    os.makedirs(d, exist_ok=True)
    for name, data in {"SKILL.md": "---\nname: x\n---\n", **(files or {})}.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
    return d


@pytest.mark.parametrize("ok,have,verdict", [
    (True, True, "ok"), (True, False, "ok"),
    (False, True, "stale"),      # update failed but a copy is checked out
    (False, False, "missing"),   # the only case that stops a launch
])
def test_decide(ok, have, verdict):
    assert skills.decide(ok, have) == verdict


def test_find_skills_finds_nested_skill_dirs_and_skips_hidden_and_bad_names(tmp_path):
    base = str(tmp_path)
    make_skill(base, "okf/skills/okf-generate")
    make_skill(base, "okf/skills/okf-consume")
    make_skill(base, "okf/.git/skills/hidden")
    make_skill(base, "okf/skills/Bad Name")
    make_skill(base, "okf/skills/okf-consume/inner/nested")   # inside a skill: not a skill
    os.makedirs(os.path.join(base, "okf", "skills", "no-skill-md"))
    assert sorted(skills.find_skills(base)) == ["okf-consume", "okf-generate"]


def test_find_skills_on_an_uninitialised_submodule_dir_is_empty(tmp_path):
    assert skills.find_skills(str(tmp_path)) == {}
    assert skills.find_skills(str(tmp_path / "missing")) == {}


def test_skill_files_map_into_the_container_and_keep_the_exec_bit(tmp_path):
    base = str(tmp_path)
    d = make_skill(base, "okf/skills/okf-consume",
                   {"references/trust.md": "t", "scripts/scan.py": "#!/usr/bin/env python3\n"})
    os.chmod(os.path.join(d, "scripts", "scan.py"), 0o755)
    got = {p: m for p, _, m in skills.skill_files(base)}
    root = skills.CONTAINER_SKILLS + "/okf-consume"
    assert got == {f"{root}/SKILL.md": "0644", f"{root}/references/trust.md": "0644",
                   f"{root}/scripts/scan.py": "0755"}


def test_skill_files_never_follow_symlinks_or_ship_hidden_cache_or_huge_files(tmp_path):
    base = str(tmp_path)
    d = make_skill(base, "okf/skills/s", {".secret": "x", "__pycache__/a.pyc": "x",
                                          "big.bin": "x" * (skills.MAX_FILE + 1)})
    secret = tmp_path / "host-secret"
    secret.write_text("private")
    os.symlink(secret, os.path.join(d, "link.md"))
    os.symlink(tmp_path, os.path.join(d, "linkdir"))
    assert [p for p, _, _ in skills.skill_files(base)] == [skills.CONTAINER_SKILLS + "/s/SKILL.md"]


def test_skill_files_cap_the_total(tmp_path, monkeypatch):
    make_skill(str(tmp_path), "o/skills/s", {"a.md": "xxxx"})
    monkeypatch.setattr(skills, "MAX_TOTAL", 10)
    with pytest.raises(ValueError):
        skills.skill_files(str(tmp_path))


def fake_context(monkeypatch, result=None, exc=None):
    calls = []

    class Ctx:
        def run(self, cmd, **kw):
            calls.append((cmd, kw))
            if exc:
                raise exc
            return result
    monkeypatch.setattr(skills, "Context", Ctx)
    return calls


def res(exited=0, out="", err=""):
    return types.SimpleNamespace(exited=exited, stdout=out, stderr=err)


def test_refresh_runs_one_quoted_non_prompting_submodule_update(monkeypatch):
    calls = fake_context(monkeypatch, res())
    assert skills.refresh("/r oot", "/r oot/claude_sandbox/data/skills") == (True, "")
    cmd, kw = calls[0]
    assert cmd == ("git -C '/r oot' submodule update --init --remote --depth 1 "
                   "-- claude_sandbox/data/skills")
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0" and kw["timeout"] == skills.TIMEOUT
    assert kw["warn"] is True and kw["in_stream"] is False


@pytest.mark.parametrize("kw,needle", [
    (dict(result=res(1, err="fatal: unable to access")), "unable to access"),
    (dict(result=res(127)), "not installed"),
    (dict(exc=CommandTimedOut(types.SimpleNamespace(command="git"), 90)), "timed out"),
])
def test_refresh_reports_failure_instead_of_raising(monkeypatch, kw, needle):
    fake_context(monkeypatch, **kw)
    ok, detail = skills.refresh("/r")
    assert not ok and needle in detail


def test_the_real_submodule_is_declared_and_provides_the_okf_skills(repo_root):
    """The reference is correct: .gitmodules points at the OKF repo, and (when
    the submodule is checked out) it holds both skills."""
    with open(os.path.join(repo_root, ".gitmodules")) as f:
        text = f.read()
    assert "claude_sandbox/data/skills/open-knowledge-format" in text
    assert "https://github.com/open-knowledge-format/open-knowledge-format" in text
    found = skills.find_skills(skills.SKILLS_DIR)
    if found:   # CI checks out without submodules
        assert {"okf-generate", "okf-consume"} <= set(found)


def test_cli_stops_only_when_no_skill_is_available(monkeypatch, capsys):
    monkeypatch.setattr(skills, "refresh", lambda: (False, "offline"))
    monkeypatch.setattr(skills, "find_skills", lambda: {})
    assert cli._skills_ready() is False
    assert "git submodule update --init" in capsys.readouterr().err
    monkeypatch.setattr(skills, "find_skills", lambda: {"okf-consume": "/x"})
    assert cli._skills_ready() is True            # stale copy: warn and go on
    assert "using the copy already checked out" in capsys.readouterr().err
    monkeypatch.setattr(skills, "refresh", lambda: (True, ""))
    assert cli._skills_ready() is True


def test_install_skills_is_one_mkdir_and_one_tar_stream(make_args, monkeypatch, tmp_path):
    files = [(skills.CONTAINER_SKILLS + "/okf-consume/SKILL.md", b"s", "0644"),
             (skills.CONTAINER_SKILLS + "/okf-consume/scripts/scan.py", b"p", "0755")]
    monkeypatch.setattr(skills, "skill_files", lambda: files)
    monkeypatch.setattr(skills, "find_skills", lambda: {"okf-consume": "/x"})
    calls = []
    box = sandbox.Sandbox(make_args(repo="o/r"))
    box.box = types.SimpleNamespace(
        user=lambda *a, **k: calls.append(("user", a)),
        root_argv=lambda argv, input=None, **k: calls.append(("root", argv, input)))
    box.install_skills()
    assert [c[0] for c in calls] == ["user", "root"]
    assert skills.CONTAINER_SKILLS + "/okf-consume/scripts" in calls[0][1][0]
    assert calls[1][1] == ["tar", "-xzf", "-", "-C", "/"]
    with tarfile.open(fileobj=io.BytesIO(calls[1][2]), mode="r:gz") as tar:
        assert {m.name for m in tar.getmembers()} == {p.lstrip("/") for p, _, _ in files}


def test_install_skills_does_nothing_without_skills(make_args, monkeypatch):
    monkeypatch.setattr(skills, "skill_files", lambda: [])
    box = sandbox.Sandbox(make_args(repo="o/r"))
    box.box = types.SimpleNamespace()     # any call would raise
    box.install_skills()
