"""The local backend: ids, which refs may come back, and the git argv.

The real-git tests build bundles with the very argv the backend uses, and
import them into a scratch "primary" repo, so the trust boundary (only
refs/heads/sbx/<session>/* can be created) is checked against git itself.
"""
import io
import os
import pathlib
import subprocess

import pytest

import claude_sandbox as cs
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


# --------------------------------------------------------------------------- #
# the backend end to end, with the "container" run locally
# --------------------------------------------------------------------------- #
class LocalBox:
    """Stands in for a Container: runs every command here, as us. The
    backend only ever reaches the container through these four methods."""

    def __init__(self, home):
        self.home, self.calls = home, []

    def user_cmd(self, *argv, env=None):
        return list(argv)

    def user_argv(self, argv, env=None, cwd=None, **kw):
        self.calls.append(argv)
        kw.pop("capture", None)
        return subprocess.run(argv, cwd=cwd or self.home, check=True,
                              capture_output=True, **kw)

    def user(self, script, **kw):
        return self.user_argv(["bash", "-c", script])


@pytest.fixture
def local(tmp_path, monkeypatch, make_args):
    """A primary repo on `main` and a LocalBackend over it, with git's global
    config shut out and the key store under tmp."""
    for k, v in dict(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                     GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                     GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e").items():
        monkeypatch.setenv(k, v)
    primary = tmp_path / "my proj"
    git("init", "-q", "-b", "main", str(primary))
    (primary / "a.txt").write_text("1")
    (primary / "claude_sandbox_deps.yaml").write_text("apt: [jq]\n")
    git("add", "-A", cwd=primary)
    git("commit", "-q", "-m", "base", cwd=primary)
    home = tmp_path / "home"
    home.mkdir()
    env = {"CLAUDE_SANDBOX_STATE_DIR": str(tmp_path / "state")}
    args = make_args(local=lb.resolve_local(str(primary / ".")))
    return lb.LocalBackend(args, environ=env, home=str(home)), LocalBox(str(home))


def test_resolve_local_finds_the_top_level_and_refuses_non_repos(tmp_path):
    (tmp_path / "r" / "sub").mkdir(parents=True)
    git("init", "-q", str(tmp_path / "r"))
    assert lb.resolve_local(str(tmp_path / "r" / "sub")) == str(tmp_path / "r")
    with pytest.raises(ValueError):
        lb.resolve_local(str(tmp_path))
    with pytest.raises(ValueError):
        lb.resolve_local(str(tmp_path / "nope"))


def test_base_is_the_checked_out_branch_unless_given(local):
    be, _ = local
    assert be.check_host() is None
    assert be.base == "main" and len(be.base_sha) == 40
    be2 = lb.LocalBackend(type(be.args)(**{**vars(be.args), "base": "nope"}))
    assert "no branch" in be2.check_host()
    be3 = lb.LocalBackend(type(be.args)(**{**vars(be.args), "base": "-x"}))
    assert "not a valid branch" in be3.check_host()


def test_a_detached_head_needs_base(local):
    be, _ = local
    git("checkout", "-q", "--detach", cwd=be.primary)
    assert "detached" in be.check_host()


def test_files_are_read_from_the_base_commit_only(local):
    be, _ = local
    assert be.read_file("claude_sandbox_deps.yaml") == b"apt: [jq]\n"
    assert be.read_file("missing.yaml") is None
    with open(os.path.join(be.primary, "untracked.yaml"), "w") as f:
        f.write("x")
    assert be.read_file("untracked.yaml") is None   # the work tree is not read


def test_populate_clones_the_base_onto_the_work_branch(local):
    be, box = local
    wd = be.populate(box, S)
    assert wd == os.path.join(box.home, "my-proj")
    assert git("branch", "--show-current", cwd=wd, text=True).stdout.strip() == f"sbx/{S}/work"
    assert git("rev-parse", "origin/HEAD", cwd=wd, text=True).stdout.strip() == be.base_sha
    assert git("fetch", "-q", "origin", cwd=wd).returncode == 0   # offline, from the bundle
    assert "push" in " ".join(be.access_lines(wd, f"sbx/{S}/work"))


def test_round_trip_through_the_backend(local, capsys):
    be, box = local
    wd = be.populate(box, S)
    commit(pathlib.Path(wd), "w.txt", f"sbx/{S}/work")
    commit(pathlib.Path(wd), "evil.txt", "main")
    be.export(box, S)
    assert be.exported and os.stat(be.exported).st_mode & 0o077 == 0
    main_before = git("rev-parse", "main", cwd=be.primary, text=True).stdout
    be.land(S)
    assert git("rev-parse", "main", cwd=be.primary, text=True).stdout == main_before
    assert git("show", f"sbx/{S}/work:w.txt", cwd=be.primary, text=True).stdout == "w.txt"
    err = capsys.readouterr().err
    assert f"sbx/{S}/work  1 commit" in err and "no signing key recorded" in err
    assert not os.path.exists(be.exported)   # imported, so not kept


def test_nothing_new_means_nothing_to_import(local, capsys):
    be, box = local
    be.populate(box, S)
    be.export(box, S)
    assert be.exported is None and not be.export_failed
    be.land(S)   # a no-op
    assert "nothing to import" in capsys.readouterr().err


def test_export_failure_is_reported_never_raised(local, capsys):
    be, box = local
    be.populate(box, S)
    be.base_sha = "f" * 40   # not in the repo: git bundle create fails
    be.export(box, S)
    assert be.export_failed and be.exported is None
    assert "couldn't export" in capsys.readouterr().err


def test_signed_commits_pass_provenance_in_the_report(local, tmp_path, capsys):
    be, box = local
    key = tmp_path / "sign"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   check=True)
    sandbox_provenance.save_key(be.key_repo, S, (tmp_path / "sign.pub").read_text(),
                                root=be.key_root)
    wd = be.populate(box, S)
    for k, v in (("gpg.format", "ssh"), ("user.signingkey", str(key)),
                 ("commit.gpgsign", "true")):
        git("config", k, v, cwd=wd)
    commit(pathlib.Path(wd), "w.txt", f"sbx/{S}/work")
    be.export(box, S)
    be.land(S)
    err = capsys.readouterr().err
    assert "1 commit signed by session" in err and "✔" in err


def test_report_flags_privileged_files_and_sanitises_session_text():
    row = dict(branch=f"sbx/{S}/work", commits=2, provenance_ok=True,
               provenance="2 commits signed", checker_errors=0,
               sensitive=[".github/workflows/x.yml"],
               synopsis=("abc1234", "Title \x1b[31mred @someone", []),
               not_verified=[("abc1234", "Not verified: on real hw")])
    text = lb.format_report("/p", "main", S, [row])
    assert "privilege" in text and ".github/workflows/x.yml" in text
    assert "\x1b" not in text and "@someone" not in text
    assert "Not verified: on real hw" in text
    assert "⚠" not in text.split("\n")[1]   # the branch line itself is clean


# --------------------------------------------------------------------------- #
# the launcher's side: flags and backend choice
# --------------------------------------------------------------------------- #
@pytest.fixture
def a_repo(tmp_path):
    git("init", "-q", "-b", "main", str(tmp_path / "r"))
    return str(tmp_path / "r")


def test_local_picks_the_local_backend(a_repo):
    args = cs.normalize_args(cs.build_parser().parse_args(["--local", a_repo]))
    assert args.local == os.path.realpath(a_repo) and args.repo is None
    assert isinstance(cs.make_backend(args), lb.LocalBackend)
    assert not isinstance(cs.make_backend(cs.normalize_args(
        cs.build_parser().parse_args(["o/r"]))), lb.LocalBackend)


@pytest.mark.parametrize("extra", [["o/r"], ["--repo", "o/r"], ["--account-key"],
                                   ["--land-all"], ["--read-only"]])
def test_local_refuses_github_only_modes(a_repo, extra):
    with pytest.raises(ValueError, match="--local can't be combined"):
        cs.normalize_args(cs.build_parser().parse_args(["--local", a_repo, *extra]))


def test_base_needs_local():
    with pytest.raises(ValueError, match="--base"):
        cs.normalize_args(cs.build_parser().parse_args(["o/r", "--base", "dev"]))


def test_the_background_refresh_keeps_the_local_source(make_args):
    argv = cs.refresh_argv(make_args(local="/p", base="dev"), "py", "s")
    assert argv[argv.index("--local") + 1] == "/p" and "--base" in argv


def test_no_github_rewrite_without_a_key():
    assert "insteadOf" not in cs.git_config_script("a", "b", github_ssh=False)
    assert "insteadOf" in cs.git_config_script("a", "b")


# --------------------------------------------------------------------------- #
# kept work volume: released only once the work is safe
# --------------------------------------------------------------------------- #
def test_land_keeps_the_volume_when_the_export_failed(local, capsys):
    be, box = local
    be.populate(box, S)
    be.work_kept, be.export_failed = True, True
    assert be.land(S) is False
    assert "--recover" in capsys.readouterr().err


def test_land_frees_the_volume_after_an_import_or_when_nothing_is_new(local):
    be, box = local
    wd = be.populate(box, S)
    be.export(box, S)
    assert be.land(S) is True                 # nothing new
    commit(pathlib.Path(wd), "w.txt", f"sbx/{S}/work")
    be.export(box, S)
    assert be.land(S) is True                 # imported


def test_keep_work_metadata_lets_recover_find_everything(local):
    be, _ = local
    meta = be.keep_work(S)
    assert meta == {"source": be.primary, "session": S, "base": be.base_sha,
                    "workdir": be.workdir()}


def test_access_lines_say_whether_work_survives_a_crash(local):
    be, _ = local
    be.resolve()
    assert "is lost" in " ".join(be.access_lines("/w", None))
    be.work_kept = True
    assert "recover" in " ".join(be.access_lines("/w", None))


def test_sandbox_releases_kept_volumes_only_when_the_backend_says_so(make_args):
    class Vols:
        released = 0

        def release(self):
            Vols.released += 1

    class Be:
        def __init__(self, ok):
            self.ok = ok

        def land(self, session):
            return self.ok
    for ok, n in ((False, 0), (True, 1)):
        Vols.released = 0
        sb = cs.Sandbox(make_args(repo="o/r"))
        sb.backend, sb.volumes = Be(ok), Vols()
        sb.land()
        assert Vols.released == 0            # not beside the background delete
        sb.wait_volumes()
        assert Vols.released == n
