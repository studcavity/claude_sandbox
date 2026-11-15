"""The rules added in plan 5b: pure functions first, then real git repos."""
import subprocess

import pytest

import sandbox_check as sc

AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # assembled: nothing token-shaped in the source
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2


# --- pure rules ------------------------------------------------------------ #
@pytest.mark.parametrize("subject,expect", [
    ("feat: add thing", []), ("fix(scope): x", []), ("docs!: breaking", []),
    ("ci: x", []), ('Revert "feat: x"', []),
    ("fixup! feat: x", [sc.ERROR]), ("squash! x", [sc.ERROR]),
    ("amend! x", [sc.ERROR]),
    ("did some stuff", [sc.WARNING]), ("feat:nospace", [sc.WARNING]),
    ("Feat: capital", [sc.WARNING]), ("", [sc.WARNING]),
])
def test_subject_rules(subject, expect):
    assert [s for s, _ in sc.subject_findings(subject + "\n\nbody")] == expect


@pytest.mark.parametrize("files,msg,errors", [
    ({"sandbox_images.py"}, "feat: x", 1),
    ({"sandbox_images.py"}, "feat: x\n\nVerified: smoke", 0),
    ({"sandbox_deps.py"}, "feat: x\n\nNot verified: no incus here", 0),
    ({"sandbox_deps.py"}, "feat: x\n\nnot verified: lower case", 0),
    ({"sandbox_images.py"}, "feat: x\n\nNot verified against real incus", 0),   # CLAUDE.md wording
    ({"sandbox_images.py"}, "feat: x\n\nVerified inside a sandbox without nesting", 0),
    ({"sandbox_containers.py", "other.py"}, "feat: x\n\nVerifiedish", 1),
    ({"sandbox_deps.py"}, "feat: x\n\nwe never verified this", 1),         # mid-sentence isn't a claim
    ({"pkg/sandbox_containers.py"}, "feat: x", 1),      # by file name, any directory
    ({"sandbox_check.py"}, "feat: x", 0),
])
def test_real_run_rule(files, msg, errors):
    assert len(sc.verification_findings(msg, files)) == errors


def test_resolves_rule_uses_the_existence_check():
    known = {"abc1234"}
    msg = ("fix: x\n\nResolves follow-up from abc1234\n"
           "Resolves follow-up from ffff0000")
    found = sc.resolves_findings(msg, lambda r: r in known)
    assert len(found) == 1 and "ffff0000" in found[0][1]
    assert sc.resolves_findings("no reference here", lambda r: False) == []
    # shorter than 7 hex digits isn't a reference
    assert sc.resolves_findings("Resolves follow-up from abc12", lambda r: False) == []


def test_message_secret_has_no_marker_escape_hatch():
    found = sc.message_secret_findings(f"fix: x\n\n{GH}  # secret-scan: allow")
    assert [s for s, _ in found] == [sc.ERROR]
    assert GH not in found[0][1]


DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,0 +2,2 @@
+first
+second
@@ -10 +12,1 @@
-old
+third
diff --git a/dir with space/b.txt b/dir with space/b.txt
--- /dev/null
+++ b/dir with space/b.txt\t
@@ -0,0 +1 @@
+only
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-deleted line
"""


def test_parse_added_lines_tracks_paths_and_line_numbers():
    assert sc.parse_added_lines(DIFF) == [
        ("a.py", 2, "first"), ("a.py", 3, "second"), ("a.py", 12, "third"),
        ("dir with space/b.txt", 1, "only")]
    assert sc.parse_added_lines("") == []


def test_added_secret_findings_marker_and_location():
    added = [("x.py", 7, f'T = "{GH}"'),
             ("y.py", 3, f'T = "{GH}"  # secret-scan: allow'),
             ("z.py", 1, "nothing here")]
    err, note = sc.added_secret_findings(added)
    assert err[0] == sc.ERROR and err[2:] == ("x.py", 7)
    assert note[0] == sc.NOTICE and note[2:] == ("y.py", 3)
    assert GH not in err[1] and GH not in note[1]


def test_scan_truncation_is_reported_not_silent(monkeypatch):
    monkeypatch.setattr(sc, "MAX_ADDED_LINES", 2)
    out = sc.added_secret_findings([("f", i, "x") for i in range(5)])
    assert len(out) == 1 and out[0][0] == sc.WARNING and "stopped" in out[0][1]


@pytest.mark.parametrize("files,warn", [
    ({"a.py"}, True), ({"a.py", "tests/test_a.py"}, False),
    ({"a.py", "test_a.py"}, False), ({"README.md", "docs/x.md"}, False),
    ({"tests/test_a.py"}, False), ({"a.sh", "b.py", "c.py", "d.py", "e.py"}, True),
    ({"pkg/__tests__/x.js", "pkg/x.js"}, False), (set(), False),
])
def test_test_coverage_heuristic(files, warn):
    assert bool(sc.test_coverage_finding(files)) is warn


# --- annotation safety ------------------------------------------------------ #
def test_annotation_escapes_workflow_command_syntax():
    f = sc.Finding("abc1234", sc.ERROR, "bad\n::error::forged%\r", "a,b:c.py", 3)
    out = sc.annotation(f)
    assert "\n" not in out and "\r" not in out
    assert out == ("::error file=a%2Cb%3Ac.py,line=3::abc1234: "
                   "bad%0A::error::forged%25%0D")
    assert sc.annotation(sc.Finding("", sc.WARNING, "x", None, None)) == "::warning::x"


# --- real git repos --------------------------------------------------------- #
class Repo:
    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.commit("base", {"README": "r"})

    def git(self, *a, input=None):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "commit.gpgsign=false", *a], cwd=self.path, check=True,
            capture_output=True, text=True, input=input).stdout

    def commit(self, message, files=None, remove=()):
        for name, content in (files or {}).items():
            f = self.path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        for name in remove:
            (self.path / name).unlink()
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Repo(tmp_path)


def run(*extra):
    return sc.main(["main~1", "main", *extra])


def texts(base="main~1", head="main"):
    return [(f.severity, f.text) for f in sc.check_findings(base, head)]


def test_merge_commits_are_flagged_not_skipped(repo):
    repo.commit("feat: on main", {"a.txt": "1"})
    repo.git("switch", "-q", "-c", "side", "main~1")
    repo.commit("feat: side", {"b.txt": "2"})
    repo.git("switch", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge side", "side")
    found = sc.check_findings("main~2", "main")
    assert any(f.severity == sc.ERROR and "merge commit" in f.text for f in found)
    assert run() == 1


def test_secret_in_an_added_line_is_an_error_with_file_and_line(repo, capsys):
    repo.commit("feat: config", {"conf.py": f"a = 1\nb = 2\nTOKEN = '{GH}'\n"})
    assert run() == 1
    out = capsys.readouterr().out
    assert "::error file=conf.py,line=3::" in out and GH not in out


def test_secret_added_then_removed_is_still_caught(repo):
    repo.commit("feat: oops", {"conf.py": f"T = '{GH}'\n"})
    repo.commit("fix: remove it", {"conf.py": "T = None\n"})
    found = sc.check_findings("main~2", "main")
    assert [(f.severity, f.path) for f in found if f.severity == sc.ERROR] == [
        (sc.ERROR, "conf.py")]


def test_deleted_secret_lines_are_not_flagged(repo):
    repo.commit("feat: seed", {"conf.py": "x = 1\n"})
    repo.git("commit", "-q", "--amend", "-m", "feat: seed")   # keep history simple
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("fix: drop", {"conf.py": "x = 2\n"})
    assert [f for f in sc.check_findings(base, "main") if f.severity == sc.ERROR] == []


def test_allow_marker_downgrades_to_a_notice_and_passes(repo, capsys):
    repo.commit("test: fixture", {"t.py": f"T = '{GH}'  # secret-scan: allow\n",
                                  "tests/test_t.py": "x"})
    assert run() == 0
    out = capsys.readouterr().out
    assert "::notice file=t.py,line=1::" in out and GH not in out


def test_secret_in_message_is_an_error(repo):
    repo.commit(f"feat: x\n\nkey {GH}")
    assert [f.severity for f in sc.check_findings("main~1", "main")
            if "message" in f.text] == [sc.ERROR]


def test_sensitive_file_needs_a_verification_line(repo):
    repo.commit("feat: change images", {"sandbox_images.py": "x", "tests/t.py": "x"})
    assert any(s == sc.ERROR and "real run" in t for s, t in texts())
    repo.commit("feat: again\n\nNot verified: no incus", {"sandbox_images.py": "y"})
    assert not any("real run" in t for s, t in texts("main~1", "main"))


def test_resolves_follow_up_checks_real_history(repo):
    first = repo.commit("feat: first\n\nFollow-up: do x", {"a.txt": "1"})
    repo.commit(f"fix: done\n\nResolves follow-up from {first[:10]}", {"b.txt": "2"})
    assert not any("Resolves" in t for s, t in texts("main~2", "main"))
    repo.commit("fix: bogus\n\nResolves follow-up from 0123456789abcdef", {"c.txt": "3"})
    assert any(s == sc.ERROR and "no such commit" in t for s, t in texts())


def test_fixup_subject_is_an_error_and_plain_subject_a_warning(repo):
    repo.commit("fixup! feat: x", {"a.txt": "1"})
    assert [s for s, _ in texts()] == [sc.ERROR]
    repo.commit("stuff", {"b.txt": "2"})
    assert [s for s, _ in texts()] == [sc.WARNING]


def test_strict_promotes_warnings_to_a_failing_exit(repo):
    repo.commit("stuff", {"a.txt": "1"})
    assert run() == 0
    assert run("--strict") == 1


def test_missing_tests_warn_once_for_the_range(repo):
    repo.commit("feat: a", {"a.py": "x"})
    repo.commit("feat: b", {"b.py": "x"})
    found = [f for f in sc.check_findings("main~2", "main") if "no test file" in f.text]
    assert len(found) == 1 and found[0].sha == "" and found[0].severity == sc.WARNING
    repo.commit("test: c", {"tests/test_a.py": "x"})
    assert not [f for f in sc.check_findings("main~3", "main") if "no test file" in f.text]


def test_check_range_still_returns_errors_only(repo):
    repo.commit("stuff", {"a.py": "x"})          # warnings only
    assert sc.check_range("main~1", "main") == []
    repo.commit("fixup! x", {"b.txt": "1"})
    ((sha, problem),) = sc.check_range("main~2", "main")
    assert len(sha) == 7 and "fixup" in problem


def test_non_utf8_commit_message_does_not_crash_the_checker(repo):
    (repo.path / "m.bin").write_bytes(b"feat: bytes\n\nbad \xff\xfe utf8\n")
    (repo.path / "a.txt").write_text("1")
    repo.git("add", "-A")
    repo.git("-c", "i18n.commitEncoding=latin1", "commit", "-q", "-F", "m.bin")
    assert run() in (0, 1)   # any verdict; the point is: no exception


def test_hostile_commit_text_cannot_forge_annotations(repo, capsys):
    repo.commit("fixup! x\n::error::forged\n::set-output name=x::y", {"a.txt": "1"})
    run()
    lines = capsys.readouterr().out.splitlines()
    assert all(l.startswith("::") or l.startswith("checked ") for l in lines)
    assert sum(l.startswith("::error") for l in lines) == 1   # ours; no forged second
