"""The local backend: ids, which refs may come back, and the git argv.

The real-git tests build bundles with the very argv the backend uses, and
import them into a scratch "primary" repo, so the trust boundary (only
refs/heads/sbx/<session>/* can be created) is checked against git itself.
"""
import io
import os
import subprocess

import pytest

import sandbox_backend_local as lb
import sandbox_provenance

S = "quirky-hopper"
SHA = "a" * 40


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def test_repo_id_is_stable_readable_and_unique_per_path():
    a = lb.repo_id("/home/u/src/proj")
    assert a == lb.repo_id("/home/u/src/proj")
    assert a.startswith("local/proj-") and len(a.rsplit("-", 1)[1]) == 8
    assert a != lb.repo_id("/home/u/other/proj")


@pytest.mark.parametrize("path", ["/x/..", "/x/...weird", "/x/a b$c", "/x/" + "n" * 300,
                                  "/", "/x/-.-"])
def test_repo_id_always_fits_the_key_store(path, tmp_path):
    rid = lb.repo_id(path)
    # key_path raises on anything the key store can't hold safely
    p = sandbox_provenance.key_path(rid, S, str(tmp_path))
    assert p.startswith(str(tmp_path)) and ".." not in rid


def test_local_keys_never_share_a_directory_with_github_owners(tmp_path):
    env = {"CLAUDE_SANDBOX_STATE_DIR": str(tmp_path)}
    gh_root, local_root = sandbox_provenance.store_root(env), lb.key_root(env)
    assert gh_root != local_root
    assert not local_root.startswith(gh_root + os.sep)


# --------------------------------------------------------------------------- #
# which refs may come back
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ref", [f"refs/heads/sbx/{S}/work",
                                 f"refs/heads/sbx/{S}/fix/parser",
                                 f"refs/heads/sbx/{S}/v1.2_x"])
def test_this_sessions_branches_are_accepted(ref):
    assert lb.acceptable_ref(ref, S)


@pytest.mark.parametrize("ref", [
    "refs/heads/main", "refs/heads/sbx/other/work", "refs/tags/sbx/x",
    f"refs/heads/sbx/{S}", f"refs/heads/sbx/{S}/", f"refs/heads/sbx/{S}/../main",
    f"refs/heads/sbx/{S}/a..b", f"refs/heads/sbx/{S}/.hidden",
    f"refs/heads/sbx/{S}/x.lock", f"refs/heads/sbx/{S}/x.", f"refs/heads/sbx/{S}//x",
    f"refs/heads/sbx/{S}/a b", f"refs/heads/sbx/{S}/x@{{1}}", f"refs/heads/sbx/{S}/-x",
    f"refs/remotes/origin/sbx/{S}/work", f"refs/heads/sbx/{S}/" + "/".join("a" * 9),
    f"refs/heads/SBX/{S}/work", f"refs/heads/sbx/{S}/é",
])
def test_everything_else_is_rejected(ref):
    assert not lb.acceptable_ref(ref, S)


def test_a_bad_session_name_accepts_nothing():
    assert not lb.acceptable_ref("refs/heads/sbx/a/b/work", "a/b")
    assert not lb.acceptable_ref("refs/heads/sbx//work", "")


def test_parse_heads_splits_accepted_from_rejected():
    text = (f"{SHA} refs/heads/sbx/{S}/work\n"
            f"{SHA} refs/heads/main\n"
            "garbage line\n"
            f"{'b' * 64} refs/heads/sbx/{S}/sha256\n"
            f"{SHA} refs/heads/sbx/{S}/work\n"   # duplicate
            "\n")
    ok, bad = lb.parse_heads(text, S)
    assert ok == [(SHA, f"refs/heads/sbx/{S}/work"),
                  ("b" * 64, f"refs/heads/sbx/{S}/sha256")]
    assert len(bad) == 3 and any("main" in b for b in bad)


def test_parse_heads_caps_the_number_of_refs():
    text = "".join(f"{SHA} refs/heads/sbx/{S}/b{i}\n"
                   for i in range(lb.MAX_BUNDLE_REFS + 5))
    ok, bad = lb.parse_heads(text, S)
    assert len(ok) == lb.MAX_BUNDLE_REFS and len(bad) == 5


# --------------------------------------------------------------------------- #
# argv
# --------------------------------------------------------------------------- #
def test_fetch_argv_is_explicit_non_forced_and_fscked():
    argv = lb.fetch_argv("/p", "/s/x.bundle", [f"refs/heads/sbx/{S}/work"])
    assert argv[:3] == ["git", "-C", "/p"]
    assert "transfer.fsckObjects=true" in argv and "--no-tags" in argv
    assert argv[-2:] == ["/s/x.bundle",
                         f"refs/heads/sbx/{S}/work:refs/heads/sbx/{S}/work"]
    assert not any(a.startswith("+") or "*" in a for a in argv)


@pytest.mark.parametrize("refs", [[], ["refs/tags/v1"], ["HEAD"]])
def test_fetch_argv_refuses_odd_input(refs):
    with pytest.raises(ValueError):
        lb.fetch_argv("/p", "/b", refs)


def test_bundle_out_argv_validates_its_inputs():
    assert lb.bundle_out_argv("/w", S, SHA)[-2:] == [f"--branches=sbx/{S}/*", f"^{SHA}"]
    with pytest.raises(ValueError):
        lb.bundle_out_argv("/w", S, "HEAD")
    with pytest.raises(ValueError):
        lb.bundle_out_argv("/w", "a/*", SHA)


def test_copy_capped_stops_past_the_limit():
    out = io.BytesIO()
    assert lb.copy_capped(io.BytesIO(b"x" * 10), out, 10, chunk=3) == 10
    with pytest.raises(lb.LocalError):
        lb.copy_capped(io.BytesIO(b"x" * 11), io.BytesIO(), 10, chunk=3)


# --------------------------------------------------------------------------- #
# against real git
# --------------------------------------------------------------------------- #
def git(*args, cwd=None, **kw):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, **kw)


@pytest.fixture
def repos(tmp_path):
    """A primary on `main`, and a 'session' clone made from its bundle."""
    primary, work = tmp_path / "primary", tmp_path / "work"
    git("init", "-q", "-b", "main", str(primary))
    (primary / "a.txt").write_text("1")
    git("add", "-A", cwd=primary)
    git("commit", "-q", "-m", "base", cwd=primary)
    base = git("rev-parse", "HEAD", cwd=primary, text=True).stdout.strip()
    inb = tmp_path / "in.bundle"
    with open(inb, "wb") as f:
        f.write(subprocess.run(lb.bundle_in_argv(str(primary), "main"),
                               check=True, capture_output=True).stdout)
    git("clone", "-q", "-b", "main", str(inb), str(work))
    return primary, work, base, tmp_path


def commit(repo, name, branch):
    git("switch", "-q", "-C", branch, cwd=repo)
    (repo / name).write_text(name)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", name, cwd=repo)


def bundle_out(work, base, path, extra=()):
    """The backend's argv, plus refs a hostile session might sneak in."""
    argv = lb.bundle_out_argv(str(work), S, base) + list(extra)
    with open(path, "wb") as f:
        subprocess.run(argv, check=True, stdout=f)
    return path


def test_round_trip_imports_only_this_sessions_branches(repos):
    primary, work, base, tmp = repos
    commit(work, "w.txt", f"sbx/{S}/work")
    commit(work, "evil.txt", "main")               # moves the session's main
    commit(work, "other.txt", "sbx/other/work")    # another session's branch
    b = bundle_out(work, base, tmp / "out.bundle",
                   extra=["main", "sbx/other/work"])
    heads = git("bundle", "list-heads", str(b), text=True).stdout
    ok, bad = lb.parse_heads(heads, S)
    assert [r for _, r in ok] == [f"refs/heads/sbx/{S}/work"]
    assert any("main" in x for x in bad) and any("other" in x for x in bad)

    before_main = git("rev-parse", "main", cwd=primary, text=True).stdout
    git(*lb.fetch_argv(str(primary), str(b), [r for _, r in ok])[1:])
    assert git("rev-parse", "main", cwd=primary, text=True).stdout == before_main
    branches = git("for-each-ref", "--format=%(refname)", cwd=primary,
                   text=True).stdout.split()
    assert branches == ["refs/heads/main", f"refs/heads/sbx/{S}/work"]
    assert git("show", f"sbx/{S}/work:w.txt", cwd=primary, text=True).stdout == "w.txt"


def test_a_second_import_never_rewrites_a_branch(repos):
    primary, work, base, tmp = repos
    commit(work, "w.txt", f"sbx/{S}/work")
    b1 = bundle_out(work, base, tmp / "1.bundle")
    ref = f"refs/heads/sbx/{S}/work"
    git(*lb.fetch_argv(str(primary), str(b1), [ref])[1:])
    first = git("rev-parse", ref, cwd=primary, text=True).stdout
    # the session rewrites its branch (not a fast-forward of what was imported)
    git("reset", "-q", "--hard", base, cwd=work)
    commit(work, "x.txt", f"sbx/{S}/work")
    b2 = bundle_out(work, base, tmp / "2.bundle")
    r = subprocess.run(lb.fetch_argv(str(primary), str(b2), [ref]),
                       capture_output=True)
    assert r.returncode != 0
    assert git("rev-parse", ref, cwd=primary, text=True).stdout == first


def test_the_in_bundle_carries_only_the_named_branch(repos):
    primary, _, _, tmp = repos
    commit(primary, "secret.txt", "private-branch")
    git("switch", "-q", "main", cwd=primary)
    with open(tmp / "b", "wb") as f:
        f.write(subprocess.run(lb.bundle_in_argv(str(primary), "main"),
                               check=True, capture_output=True).stdout)
    heads = git("bundle", "list-heads", str(tmp / "b"), text=True).stdout
    assert heads.split()[1::2] == ["refs/heads/main"]
