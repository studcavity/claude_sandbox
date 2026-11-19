"""session_docs.py: the OKF bundle a session reads on demand, assembled from
docs/session/common plus its backend's directory. Pure file reads; no containers."""
import io
import os
import re
import tarfile
import types

import pytest
import yaml

from claude_sandbox import render
from claude_sandbox import sandbox
from claude_sandbox import session_docs
from claude_sandbox import session_tool
from claude_sandbox.backends import github as bg
from claude_sandbox.backends import local as backends_local
from claude_sandbox.container import deps as container_deps
from claude_sandbox.container import host

KINDS = [bg.GitHubBackend.kind, backends_local.LocalBackend.kind]
HOME_DOCS = session_docs.CONTAINER_DOCS + "/"


def assembled(kind):
    """{file name: text} of the bundle a session of this kind gets."""
    return {p.removeprefix(HOME_DOCS): d.decode()
            for p, d, _ in session_docs.session_doc_files(kind)}


def frontmatter(text):
    assert text.startswith("---\n")
    return yaml.safe_load(text[4:].split("\n---\n", 1)[0])


@pytest.mark.parametrize("kind", KINDS)
def test_bundle_is_flat_under_the_container_docs_dir(kind):
    files = session_docs.session_doc_files(kind)
    assert files and all(p.startswith(HOME_DOCS) and "/" not in p[len(HOME_DOCS):]
                         for p, _, _ in files)
    assert all(m == "0644" for _, _, m in files)
    assert "index.md" in assembled(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_every_concept_is_valid_okf_and_listed_in_the_index(kind):
    docs = assembled(kind)
    root = frontmatter(docs["index.md"])
    assert root["okf_version"] == "0.3" and root["description"]
    for name, text in docs.items():
        if name == "index.md":
            continue
        fm = frontmatter(text)
        assert fm["type"] and fm["description"] and fm["title"], name
        assert f"]({name})" in docs["index.md"], f"{name} is not in the index"


@pytest.mark.parametrize("kind", KINDS)
def test_links_resolve_inside_the_assembled_bundle(kind):
    docs = assembled(kind)
    for name, text in docs.items():
        for target in re.findall(r"\]\(([^)#\s]+)\)", text):
            if "://" in target:
                continue
            assert target in docs, f"{name} links to {target}, not in the {kind} bundle"


@pytest.mark.parametrize("kind", KINDS)
def test_the_prompt_only_points_at_pages_that_exist(kind, repo_root):
    t = open(os.path.join(repo_root, "claude_sandbox", "data", "sandbox_CLAUDE.md")).read()
    md = render.render_claude_md(
        t, render.parse_probe("sudo=yes\ntools=git\nbranch=sbx/q/work"), "q", "/w",
        "o/r", False, "- d", session="q", kind=kind, access=["- A"])
    assert f"the **{kind}** backend" in md and session_docs.DOCS_PATH in md
    pages = set(re.findall(r"`([a-z-]+\.md)`", md))
    assert pages and pages <= set(assembled(kind)), pages - set(assembled(kind))


def test_session_docs_are_static_and_the_local_ones_promise_nothing_github_does():
    for kind in KINDS:
        for name, text in assembled(kind).items():
            assert not re.search(r"\$(container|session|prefix|user|home|deps|usb|mounts"
                                 r"|backend|docs|repo)\b", text), (kind, name)
    local = " ".join(assembled("local").values())
    for gone in ("deploy key", "api.github.com", "force-with-lease", "open a PR"):
        assert gone not in local, gone


def test_file_names_in_the_docs_match_the_constants():
    common = assembled("github")
    assert container_deps.DEPS_FILE in common["deps-file.md"]
    assert host.HOST_FILE in common["host-access.md"]
    assert shell_marker() in common["hazard-guard.md"]


def shell_marker():
    from claude_sandbox import shell
    return shell.SANDBOX_MARKER


def test_a_name_in_common_and_in_the_kind_is_refused(tmp_path):
    for d in ("common", "k"):
        (tmp_path / d).mkdir()
    (tmp_path / "common" / "a.md").write_text("x")
    (tmp_path / "k" / "a.md").write_text("y")
    (tmp_path / "k" / "index.md").write_text("i")
    with pytest.raises(ValueError, match="both"):
        session_docs.session_doc_files("k", src=str(tmp_path))


def test_a_kind_without_an_index_or_a_common_index_is_refused(tmp_path):
    (tmp_path / "common").mkdir()
    (tmp_path / "k").mkdir()
    with pytest.raises(ValueError, match="index.md is missing"):
        session_docs.session_doc_files("k", src=str(tmp_path))
    (tmp_path / "k" / "index.md").write_text("i")
    (tmp_path / "common" / "index.md").write_text("c")
    with pytest.raises(ValueError, match="belongs to each backend"):
        session_docs.session_doc_files("k", src=str(tmp_path))


def test_symlinks_and_hidden_files_are_not_shipped(tmp_path):
    for d in ("common", "k"):
        (tmp_path / d).mkdir()
    (tmp_path / "k" / "index.md").write_text("i")
    (tmp_path / "common" / ".hidden.md").write_text("h")
    (tmp_path / "secret.txt").write_text("host secret")
    os.symlink(tmp_path / "secret.txt", tmp_path / "common" / "link.md")
    names = [p.rsplit("/", 1)[1] for p, _, _ in
             session_docs.session_doc_files("k", src=str(tmp_path))]
    assert names == ["index.md"]


def test_install_session_docs_is_one_mkdir_and_one_tar_stream(make_args):
    calls = []
    box = sandbox.Sandbox(make_args(repo="o/r"))
    box.box = types.SimpleNamespace(
        user=lambda *a, **k: calls.append(("user", a)),
        root_argv=lambda argv, input=None, **k: calls.append(("root", argv, input)))
    box.install_session_docs()
    assert [c[0] for c in calls] == ["user", "root"]
    assert session_docs.CONTAINER_DOCS in calls[0][1][0]
    assert calls[1][1] == ["tar", "-xzf", "-", "-C", "/"]
    with tarfile.open(fileobj=io.BytesIO(calls[1][2]), mode="r:gz") as tar:
        names = {m.name for m in tar.getmembers()}
    assert (session_docs.CONTAINER_DOCS + "/index.md").lstrip("/") in names
    assert (session_docs.CONTAINER_DOCS + "/deps-file.md").lstrip("/") in names


def test_the_archive_helper_is_the_one_skills_use():
    assert session_tool.session_tool_archive([("/a/b.md", b"x", "0644")])
