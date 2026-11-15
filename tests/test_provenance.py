"""Tests for provenance: per-session signing keys and the
`sandbox/provenance` status. Signing and verification use the real git and
ssh-keygen (the code under test relies on git's own %G?/%GF checks); GitHub
is faked."""
import base64
import os
import shutil
import subprocess
import types

import pytest

from claude_sandbox import provenance as sp
needs_tools = pytest.mark.skipif(
    not (shutil.which("git") and shutil.which("ssh-keygen")),
    reason="needs git and ssh-keygen")


def ed25519_pub(seed=b"\x01"):
    blob = (len(b"ssh-ed25519").to_bytes(4, "big") + b"ssh-ed25519"
            + (32).to_bytes(4, "big") + (seed * 32)[:32])
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


# --- keys -------------------------------------------------------------------- #
def test_parse_pubkey_keeps_type_and_blob_only():
    k = ed25519_pub()
    assert sp.parse_pubkey(k + " some comment\n") == k


@pytest.mark.parametrize("text", [
    "", "ssh-ed25519 AAAA", "ssh-rsa AAAAB3NzaC1yc2E=", "ssh-ed25519 !!!",
    "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0\x05short").decode(),
    ed25519_pub() + "\nsecond line",
])
def test_parse_pubkey_refuses_anything_else(text):
    with pytest.raises(ValueError):
        sp.parse_pubkey(text)


@needs_tools
def test_fingerprint_matches_ssh_keygen(tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                    str(tmp_path / "k")], check=True)
    pub = (tmp_path / "k.pub").read_text()
    out = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "k.pub")],
                         check=True, capture_output=True, text=True).stdout
    assert sp.fingerprint(sp.parse_pubkey(pub)) == out.split()[1]


def test_key_store_round_trip_and_reuse_detection(tmp_path):
    root = str(tmp_path)
    assert sp.load_key("O/r", "quirky-hopper", root) is None
    assert sp.save_key("O/r", "quirky-hopper", ed25519_pub(), root) is False
    assert sp.load_key("o/R", "quirky-hopper", root) == ed25519_pub()  # case-insensitive repo
    assert sp.recorded_sessions("o/r", root) == {"quirky-hopper"}
    assert sp.save_key("o/r", "quirky-hopper", ed25519_pub(b"\x02"), root) is True
    assert sp.load_key("o/r", "quirky-hopper", root) == ed25519_pub(b"\x02")


@pytest.mark.parametrize("repo,session", [
    ("../x", "s"), ("o/..", "s"), ("o/r/x", "s"), ("o/r", "../evil"),
    ("o/r", "a/b"), ("", "s"),
])
def test_key_paths_cant_escape_the_store(tmp_path, repo, session):
    with pytest.raises(ValueError):
        sp.key_path(repo, session, str(tmp_path))


def test_store_root_honours_the_override_and_xdg():
    assert sp.store_root({sp.STATE_ENV: "/s"}) == "/s/signing"
    assert sp.store_root({"XDG_STATE_HOME": "/x"}) == "/x/claude_sandbox/signing"


def test_container_script_signs_every_commit_and_prints_the_public_half():
    s = sp.container_script()
    assert "commit.gpgsign true" in s and "gpg.format ssh" in s
    assert s.endswith(f"cat {sp.SIGNING_KEY}.pub")
    assert "-N ''" in s   # no passphrase prompt in an unattended exec


# --- judging (pure) ------------------------------------------------------------ #
FP = "SHA256:session"


def C(sha="a" * 40, status="G", fp=FP, parents=("p",)):
    return sp.Commit(sha, status, fp, list(parents))


def test_all_signed_by_allowed_keys_passes():
    assert sp.judge([C(), C(fp="SHA256:you")], {FP: 1, "SHA256:you": 1}) == []


@pytest.mark.parametrize("commit,why", [
    (C(status="N", fp=""), "not signed"),
    (C(status="U", fp="SHA256:other"), "isn't this session's"),
    (C(status="B"), "bad signature"),
    (C(status="E"), "can't be checked"),
    (C(fp="SHA256:other"), "isn't this session's"),
    (C(parents=("p", "q")), "merge commit"),
])
def test_each_kind_of_bad_commit_fails(commit, why):
    (problem,) = sp.judge([C(), commit], {FP: 1})
    assert why in problem[1]


def test_an_empty_range_proves_nothing():
    assert sp.judge([], {FP: 1}) == [("", "no commits to check")]


def test_description_is_bounded_and_names_the_first_problem():
    probs = [("b" * 40, "not signed")] * 3
    d = sp.describe([C()] * 3, probs, "quirky-hopper")
    assert d.startswith("bbbbbbb: not signed (+2 more)") and len(d) <= sp.DESC_MAX
    assert "3 commits signed by session quirky-hopper" in sp.describe(
        [C()] * 3, [], "quirky-hopper")


# --- real git: signing and verification ------------------------------------------ #
def git(cwd, *args, **kw):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, **kw).stdout.strip()


@pytest.fixture
def signed_repo(tmp_path):
    """A repo whose `main` has one unsigned commit; returns helpers to add
    commits signed with the session key, another key, or none."""
    keys = {}
    for name in ("session", "sibling", "user"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                        str(tmp_path / name)], check=True)
        keys[name] = sp.parse_pubkey((tmp_path / f"{name}.pub").read_text())
    repo = tmp_path / "work"
    repo.mkdir()
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, env=env, check=True,
                                    capture_output=True, text=True).stdout.strip()
    run("init", "-q", "-b", "main")
    run("-c", "user.name=u", "-c", "user.email=u@x", "commit", "-q",
        "--allow-empty", "-m", "base")
    base = run("rev-parse", "HEAD")
    run("switch", "-q", "-c", "sbx/quirky-hopper/work")

    def commit(signer, msg="c"):
        sign = (["-c", "gpg.format=ssh", "-c",
                 f"user.signingkey={tmp_path / signer}", "commit", "-S"]
                if signer else ["commit", "--no-gpg-sign"])
        run("-c", "user.name=u", "-c", "user.email=u@x", *sign, "-q",
            "--allow-empty", "-m", msg)
        return run("rev-parse", "HEAD")
    return types.SimpleNamespace(path=repo, base=base, commit=commit, keys=keys,
                                 tmp=tmp_path)


def verify(r, allowed_names):
    keys = [r.keys[n] for n in allowed_names]
    signers = r.tmp / "allowed"
    signers.write_text(sp.allowed_signers(keys))
    head = git(r.path, "rev-parse", "HEAD")
    commits = sp.read_commits(r.base, head, str(signers), str(r.path))
    return commits, sp.judge(commits, {sp.fingerprint(k): k for k in keys})


@needs_tools
def test_commits_signed_by_the_session_pass(signed_repo):
    signed_repo.commit("session")
    signed_repo.commit("session")
    commits, problems = verify(signed_repo, ["session"])
    assert len(commits) == 2 and problems == []


@needs_tools
def test_a_commit_from_a_sibling_sessions_key_fails(signed_repo):
    """The case this exists for: another deploy key pushed onto the branch."""
    signed_repo.commit("session")
    bad = signed_repo.commit("sibling")
    _, problems = verify(signed_repo, ["session"])
    assert problems == [(bad, sp.WHY["U"])]


@needs_tools
def test_an_unsigned_commit_fails(signed_repo):
    signed_repo.commit("session")
    bad = signed_repo.commit(None)
    _, problems = verify(signed_repo, ["session"])
    assert problems == [(bad, "not signed")]


@needs_tools
def test_the_users_own_signed_commits_pass(signed_repo):
    signed_repo.commit("session")
    signed_repo.commit("user")    # e.g. the user rebased or added a fixup
    assert verify(signed_repo, ["session", "user"])[1] == []


@pytest.mark.parametrize("pin", ["global", "system"])
@needs_tools
def test_git_config_outside_ours_cant_forge_a_good_signature(signed_repo, monkeypatch, pin):
    """A gpg.ssh.program that just prints "Good signature" makes git report G
    for any signed commit; neither the user's nor the system's config may
    lend us one (or a wider signers file)."""
    liar = signed_repo.tmp / "liar"
    fp = sp.fingerprint(signed_repo.keys["session"])   # claims the session's key
    liar.write_text('#!/bin/sh\necho "Good \\"git\\" signature for u@x with '
                    f'ED25519 key {fp}"\n')
    liar.chmod(0o755)
    wide = signed_repo.tmp / "wide"
    wide.write_text(sp.allowed_signers([signed_repo.keys["sibling"]]))
    cfg = signed_repo.tmp / "gitconfig"
    cfg.write_text(f"[gpg \"ssh\"]\n\tprogram = {liar}\n"
                   f"\tallowedSignersFile = {wide}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL" if pin == "global"
                       else "GIT_CONFIG_SYSTEM", str(cfg))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    signed_repo.commit("sibling")
    _, problems = verify(signed_repo, ["session"])
    assert len(problems) == 1


def test_read_commits_refuses_non_shas(tmp_path):
    with pytest.raises(sp.ProvenanceError):
        sp.read_commits("--output=/tmp/x", "a" * 40, "f", str(tmp_path))


# --- the PR flow with GitHub faked ------------------------------------------------ #
class Client:
    def __init__(self, head, head_ref="sbx/quirky-hopper/work", repo="o/r",
                 moves=False):
        self.head, self.head_ref, self.repo, self.moves = head, head_ref, repo, moves
        self.posted, self.reads = [], 0

    def pr(self, repo, number):
        self.reads += 1
        head = "f" * 40 if self.moves and self.reads > 1 else self.head
        return types.SimpleNamespace(number=number, base_sha="0" * 40,
                                     head_sha=head, head_ref=self.head_ref,
                                     head_repo=self.repo,
                                     url=f"https://github.com/o/r/pull/{number}")

    def login(self):
        return "ada"

    def post_status(self, repo, sha, state, context, description, target=None):
        self.posted.append((sha, state, context, description))

    def comment(self, repo, number, body):
        self.comments = getattr(self, "comments", []) + [(number, body)]


def fake_api(base, user_keys=()):
    def api(method, path, body=None, raw=False):
        if path == "/repos/o/r":
            return {"default_branch": "main"}
        if path == "/repos/o/r/branches/main":
            return {"commit": {"sha": base}}
        if path.startswith("/users/ada/ssh_signing_keys"):
            return [{"key": k} for k in user_keys]
        raise AssertionError(path)
    return api


def local_fetch(src):
    def fetch(url, number, base, dest):
        git(None, "clone", "-q", "--bare", str(src), dest)
    return fetch


@needs_tools
def test_post_pr_posts_success_for_a_clean_session_pr(signed_repo):
    head = signed_repo.commit("session")
    root = str(signed_repo.tmp / "store")
    sp.save_key("o/r", "quirky-hopper", signed_repo.keys["session"], root)
    client = Client(head)
    v = sp.post_pr("o/r", 3, api=fake_api(signed_repo.base), client=client,
                   root=root, fetch=local_fetch(signed_repo.path))
    assert v.state == "success"
    assert client.posted == [(head, "success", "sandbox/provenance",
                              "1 commit signed by session quirky-hopper (or you)")]


@needs_tools
def test_post_pr_posts_failure_when_a_sibling_pushed_onto_the_branch(signed_repo):
    signed_repo.commit("session")
    head = signed_repo.commit("sibling")
    root = str(signed_repo.tmp / "store")
    sp.save_key("o/r", "quirky-hopper", signed_repo.keys["session"], root)
    client = Client(head)
    sp.post_pr("o/r", 3, api=fake_api(signed_repo.base), client=client,
               root=root, fetch=local_fetch(signed_repo.path))
    ((sha, state, _, desc),) = client.posted
    assert sha == head and state == "failure" and head[:7] in desc


@needs_tools
def test_post_pr_accepts_the_users_github_signing_keys(signed_repo):
    signed_repo.commit("session")
    head = signed_repo.commit("user")
    root = str(signed_repo.tmp / "store")
    sp.save_key("o/r", "quirky-hopper", signed_repo.keys["session"], root)
    client = Client(head)
    api = fake_api(signed_repo.base, [signed_repo.keys["user"] + " laptop",
                                      "ssh-rsa AAAAB3NzaC1yc2E= ignored"])
    assert sp.post_pr("o/r", 3, api=api, client=client, root=root,
                      fetch=local_fetch(signed_repo.path)).state == "success"


def test_no_recorded_key_fails_closed(tmp_path):
    client = Client("a" * 40)
    v = sp.post_pr("o/r", 3, api=fake_api("0" * 40), client=client,
                   root=str(tmp_path))
    assert v.state == "failure" and "no signing key" in v.description
    assert client.posted[0][1] == "failure"


def test_nothing_is_posted_if_the_head_moved_while_checking(tmp_path):
    client = Client("a" * 40, moves=True)
    with pytest.raises(sp.ProvenanceError, match="moved"):
        sp.post_pr("o/r", 3, api=fake_api("0" * 40), client=client,
                   root=str(tmp_path))
    assert client.posted == []


def test_a_crash_posts_error_never_success(tmp_path):
    root = str(tmp_path)
    sp.save_key("o/r", "quirky-hopper", ed25519_pub(), root)

    def boom(*a):
        raise OSError("disk on fire")
    client = Client("a" * 40)
    v = sp.post_pr("o/r", 3, api=fake_api("0" * 40), client=client, root=root,
                   fetch=boom)
    assert v.state == "error" and client.posted[0][1] == "error"


@pytest.mark.parametrize("kw,match", [
    ({"head_ref": "feature/x"}, "sandbox branch"),
    ({"repo": "evil/fork"}, "evil/fork"),
])
def test_refuses_prs_that_arent_this_repos_session_branches(tmp_path, kw, match):
    client = Client("a" * 40, **kw)
    with pytest.raises(sp.ProvenanceError, match=match):
        sp.post_pr("o/r", 3, api=fake_api("0" * 40), client=client,
                   root=str(tmp_path))
    assert client.posted == []


def test_post_session_reports_each_pr_and_survives_failures():
    landed = [types.SimpleNamespace(outcome="opened", detail="https://x/pull/1"),
              types.SimpleNamespace(outcome="held", detail="x"),
              types.SimpleNamespace(outcome="exists", detail="https://x/pull/2")]

    def poster(repo, n, **kw):
        if n == 2:
            raise sp.ProvenanceError("head moved")
        return sp.Verdict("success", "ok", [], "a" * 40)
    out = sp.post_session("o/r", landed, poster=poster)
    assert out == [sp.Outcome(1, "success", "ok"),
                   sp.Outcome(2, "failed", "head moved")]
    text = sp.format_outcomes(out)
    assert "#1: success" in text and "--pr 2" in text and "--pr 1" not in text


def test_override_posts_success_for_the_head_and_comments():
    client = Client("a" * 40)
    v = sp.override_pr("o/r", 3, "rebased with the GitHub button; reviewed", client)
    assert v.state == "success"
    assert client.posted == [("a" * 40, "success", "sandbox/provenance",
                              "OVERRIDDEN by ada: rebased with the GitHub "
                              "button; reviewed")]
    ((n, body),) = client.comments
    assert n == 3 and "sandbox/provenance" in body and "aaaaaaa" in body


@pytest.mark.parametrize("kw,reason,match", [
    ({}, "short", "real reason"),
    ({}, "token " + "ghp_" + "x" * 40, "credential"),
    ({"head_ref": "feature/x"}, "a perfectly good reason", "sandbox branch"),
    ({"repo": "evil/fork"}, "a perfectly good reason", "evil/fork"),
    ({"moves": True}, "a perfectly good reason", "moved"),
])
def test_override_refuses_and_posts_nothing(kw, reason, match):
    client = Client("a" * 40, **kw)
    with pytest.raises(sp.ProvenanceError, match=match):
        sp.override_pr("o/r", 3, reason, client)
    assert client.posted == [] and not getattr(client, "comments", [])


def test_main_override_routes_to_override_pr(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(sp, "override_pr", lambda *a: seen.append(a) or
                        sp.Verdict("success", "d", [], "a" * 40))
    assert sp.main(["o/r", "--pr", "3", "--override", "a good reason"]) == 0
    assert seen == [("o/r", 3, "a good reason")]


# --- helpers that used to live in check (spec 01) ------------------ #
GH_TOKEN = "ghp_" + "x" * 40


def test_override_description_format_and_bounds():
    d = sp.override_description("octo", "false positive: the fixture is fake")
    assert d == "OVERRIDDEN by octo: false positive: the fixture is fake"
    long = sp.override_description("octo", "x" * 500)
    assert len(long) == sp.DESC_MAX and long.endswith("\u2026")
    assert "\n" not in sp.override_description("octo", "line one\nline two is here")
    assert sp.override_description("bad login!", "a long enough reason").startswith(
        "OVERRIDDEN by unknown:")


@pytest.mark.parametrize("reason", ["", "short", "         ", "\n" * 11])
def test_override_needs_a_real_reason(reason):
    with pytest.raises(sp.ProvenanceError, match="real reason"):
        sp.override_description("octo", reason)


def test_override_reason_with_a_credential_is_refused_without_echoing_it():
    with pytest.raises(sp.ProvenanceError) as e:
        sp.override_description("octo", f"because {GH_TOKEN} is needed")
    assert GH_TOKEN not in str(e.value) and "credential" in str(e.value)


@pytest.mark.parametrize("bad", ["--upload-pack=touch /tmp/x", "abc", "", None,
                                 "g" * 40])
def test_base_commit_id_is_validated_before_reaching_git(bad, tmp_path):
    with pytest.raises(sp.ProvenanceError, match="unexpected base"):
        sp.fetch_pr("file:///nowhere", 1, bad, str(tmp_path / "d"))


def test_a_failed_fetch_is_a_clean_error(tmp_path):
    with pytest.raises(sp.ProvenanceError, match="couldn't fetch PR #1"):
        sp.fetch_pr(str(tmp_path / "nowhere"), 1, "a" * 40, str(tmp_path / "d"))


def test_pr_numbers_only_from_opened_or_existing_prs():
    L = types.SimpleNamespace
    got = sp.pr_numbers([
        L(outcome="opened", detail="https://github.com/o/r/pull/7"),
        L(outcome="exists", detail="https://github.com/o/r/pull/8/"),
        L(outcome="held", detail=""), L(outcome="empty", detail=""),
        L(outcome="opened", detail="not-a-pr-url")])
    assert got == [7, 8]
