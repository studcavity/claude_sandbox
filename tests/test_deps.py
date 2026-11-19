"""Tests for container_deps: schema validation, cache key, archives, downloads."""
import hashlib
import io
import os
import tarfile
import zipfile

import pytest
import yaml

from claude_sandbox.container import deps as d
from claude_sandbox.container.deps import DepsError

SHA = "a" * 64


def test_sample_file_in_repo_is_valid(repo_root):
    with open(os.path.join(repo_root, d.DEPS_FILE), "rb") as f:
        deps = d.parse(f.read())
    assert deps["apt"] == ["shfmt", "gh"]
    assert deps["binaries"][0]["name"] == "shellcheck"
    assert deps["last_generated"].startswith("2026-")


@pytest.mark.parametrize("raw", [b"", "", "# only a comment\n"])
def test_empty_file_is_empty_deps(raw):
    deps = d.parse(raw)
    assert deps["apt"] == deps["scripts"] == deps["binaries"] == []
    assert d.cache_key(deps) is None
    assert d.summary(deps) == []


def test_full_document_parses():
    deps = d.parse(f"""
apt: [git, libssl-dev=3.0.13-0ubuntu3]
scripts:
  - name: tool
    url: https://example.com/i.sh
    sha256: {SHA}
    args: [--yes]
    env: {{BINDIR: /usr/local/bin, N: 3}}
binaries:
  - name: bin
    url: https://example.com/b.tgz
    sha256: {SHA}
    archive_path: dir/bin
last_generated: 2026-11-15T00:40:00Z
""")
    assert deps["apt"] == ["git", "libssl-dev=3.0.13-0ubuntu3"]
    assert deps["scripts"][0]["env"] == {"BINDIR": "/usr/local/bin", "N": "3"}
    assert deps["binaries"][0]["archive_path"] == "dir/bin"
    # unquoted YAML timestamps are normalised to a string
    assert isinstance(deps["last_generated"], str)


@pytest.mark.parametrize("doc, msg", [
    ("[1, 2]", "expected a mapping"),
    ("apt: git", "expected a list"),
    ("apt: ['-o=evil']", "invalid package"),
    ("apt: ['Git']", "invalid package"),
    ("binaries: [{name: b, url: 'https://x/b'}]", "missing key"),
    (f"binaries: [{{name: b, url: 'http://x/b', sha256: {SHA}}}]", "https"),
    (f"binaries: [{{name: b, url: 'https://u:p@x/b', sha256: {SHA}}}]", "credentials"),
    ("binaries: [{name: b, url: 'https://x/b', sha256: abc}]", "invalid sha256"),
    (f"binaries: [{{name: ../b, url: 'https://x/b', sha256: {SHA}}}]", "invalid name"),
    (f"binaries: [{{name: b, url: 'https://x/1', sha256: {SHA}}},"
     f" {{name: b, url: 'https://x/2', sha256: {SHA}}}]", "duplicate"),
    ("scripts: [{name: s, url: 'https://x/s', env: {PATH: /tmp}}]", "not allowed"),
    ("scripts: [{name: s, url: 'https://x/s', env: {LD_PRELOAD: x}}]", "not allowed"),
    ("scripts: [{name: s, url: 'https://x/s', env: {A: [1]}}]", "plain scalar"),
    ("scripts: [{name: s, url: 'https://x/s', args: x}]", "list of strings"),
    ("last_generated: yesterday", "ISO 8601"),
    ("platform: \"a\\x01b\"", "control characters"),
    ("apt: [", "invalid YAML"),
])
def test_invalid_documents_are_rejected(doc, msg):
    with pytest.raises(DepsError, match=msg):
        d.parse(doc)


def test_oversized_and_non_utf8_rejected():
    with pytest.raises(DepsError, match="larger than"):
        d.parse(b"#" * (d.MAX_DEPS_FILE + 1))
    with pytest.raises(DepsError, match="UTF-8"):
        d.parse(b"\xff\xfe")


def test_cache_key_ignores_metadata_and_key_order():
    a = d.parse("apt: [git]\nlast_generated: '2026-01-01'")
    b = d.parse("platform: x\napt: [git]")
    assert d.cache_key(a) == d.cache_key(b) is not None
    assert d.cache_key(d.parse("apt: [curl]")) != d.cache_key(a)


def test_cache_key_none_for_unpinned_script():
    deps = d.parse("scripts: [{name: s, url: 'https://x/s'}]")
    assert d.cache_key(deps) is None
    pinned = d.parse(f"scripts: [{{name: s, url: 'https://x/s', sha256: {SHA}}}]")
    assert d.cache_key(pinned) is not None


def test_summary_lists_each_section():
    deps = d.parse(f"apt: [git]\nbinaries: [{{name: b, url: 'https://x/b', sha256: {SHA}}}]")
    assert d.summary(deps) == ["apt: `git`", "binaries: `b` (/usr/local/bin)"]


# --------------------------------------------------------------------------- #
# archives
# --------------------------------------------------------------------------- #
def _tar(files, mode="w:gz"):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode=mode) as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return bio.getvalue()


def _zip(files):
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return bio.getvalue()


@pytest.mark.parametrize("pack", [_tar, lambda f: _tar(f, "w:xz"), _zip])
def test_extract_member(pack):
    data = pack({"dir/bin": b"ELF", "other": b"x"})
    assert d.extract_member(data, "dir/bin") == b"ELF"
    with pytest.raises(DepsError, match="no member"):
        d.extract_member(data, "missing")


def test_extract_member_rejects_non_archive_and_oversize():
    with pytest.raises(DepsError, match="not a tar or zip"):
        d.extract_member(b"plain bytes", "x")
    with pytest.raises(DepsError, match="acceptable size"):
        d.extract_member(_tar({"big": b"12345"}), "big", max_bytes=4)


# --------------------------------------------------------------------------- #
# downloads (network mocked)
# --------------------------------------------------------------------------- #
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def serve(monkeypatch):
    def _serve(body):
        monkeypatch.setattr(d._opener, "open", lambda req, timeout: _Resp(body))
    return _serve


def test_fetch_verifies_sha(serve):
    serve(b"hello")
    good = hashlib.sha256(b"hello").hexdigest()
    assert d.fetch("https://x/f", 100, good) == b"hello"
    with pytest.raises(DepsError, match="sha256 mismatch"):
        d.fetch("https://x/f", 100, SHA)


def test_fetch_enforces_size_cap(serve):
    serve(b"x" * 10)
    with pytest.raises(DepsError, match="larger than"):
        d.fetch("https://x/f", 5)


def test_fetch_wraps_network_errors(monkeypatch):
    def boom(req, timeout):
        raise OSError("no route")
    monkeypatch.setattr(d._opener, "open", boom)
    with pytest.raises(DepsError, match="download failed"):
        d.fetch("https://x/f", 5)


def test_redirect_to_http_is_refused():
    h = d._HttpsOnly()
    with pytest.raises(DepsError, match="non-https"):
        h.redirect_request(None, None, 302, "", {}, "http://evil/x")


# --------------------------------------------------------------------------- #
# setup: per-launch in-container scripts
# --------------------------------------------------------------------------- #
def test_setup_parses_with_defaults():
    parsed = d.parse("setup:\n  - {name: tc, path: tools/setup.sh}\n")
    assert parsed["setup"] == [{"name": "tc", "path": "tools/setup.sh", "args": [],
                           "env": {}, "timeout": 1800}]
    assert d.parse("apt: [git]")["setup"] == []


@pytest.mark.parametrize("entry", [
    "{name: a, path: /etc/passwd}", "{name: a, path: ../x.sh}",
    "{name: a, path: a/../x.sh}", "{name: a, path: ./x.sh}",
    "{name: a, path: -rf}", "{name: a, path: 'x; rm -rf ~'}",
    "{name: a, path: 'x y.sh'}", "{name: a, path: 'x\\n'}", "{name: a, path: ''}",
    "{name: a, path: x.sh, timeout: 0}", "{name: a, path: x.sh, timeout: 3601}",
    "{name: a, path: x.sh, timeout: true}", "{name: a, path: x.sh, timeout: '5'}",
    "{name: a, path: x.sh, env: {LD_PRELOAD: x}}",
    "{name: a, path: x.sh, args: [1]}", "{name: a, path: x.sh, timeout: 0}",
    "{path: x.sh}", "{name: a b, path: x.sh}",
])
def test_setup_rejects_bad_entries(entry):
    with pytest.raises(DepsError):
        d.parse(f"setup:\n  - {entry}\n")


def test_setup_rejects_duplicates_and_too_many():
    with pytest.raises(DepsError, match="duplicate"):
        d.parse("setup:\n  - {name: a, path: x}\n  - {name: a, path: y}\n")
    many = "".join(f"  - {{name: n{i}, path: s{i}.sh}}\n" for i in range(9))
    with pytest.raises(DepsError, match="at most"):
        d.parse("setup:\n" + many)


def test_setup_never_changes_the_image_cache_key():
    """Editing setup must not rebuild images: it is not baked."""
    a = d.parse("apt: [git]")
    b = d.parse("apt: [git]\nsetup:\n  - {name: a, path: x.sh}\n")
    assert d.cache_key(a) == d.cache_key(b) is not None
    # setup alone is nothing to bake
    assert d.cache_key(d.parse(
        "setup:\n  - {name: a, path: x.sh}\n")) is None


def test_summary_mentions_setup():
    parsed = d.parse("setup:\n  - {name: a, path: tools/setup.sh}\n")
    assert any("tools/setup.sh" in l and "every launch" in l
               for l in d.summary(parsed))


# --------------------------------------------------------------------------- #
# image: per-repo base OS override
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("image", ["images:ubuntu/24.04", "images:ubuntu/26.04",
                                   "ubuntu:24.04", "images:ubuntu/resolute"])
def test_image_accepts_official_ubuntu(image):
    assert d.parse(f"image: {image}\n")["image"] == image
    assert d.parse("apt: [git]")["image"] is None


@pytest.mark.parametrize("image", [
    "images:debian/13", "images:alpine/edge", "local:claude-sandbox-abc",
    "myprivate:ubuntu/24.04", "images:ubuntu/../x", "images:ubuntu/", "ubuntu:",
    "24.04", "images:ubuntu/24.04 --vm", "-c", "images:ubuntu/24.04\n--vm",
    "images:UBUNTU/24.04", "", 24.04, ["images:ubuntu/24.04"],
])
def test_image_rejects_anything_else(image):
    with pytest.raises(DepsError):
        d.parse(yaml.safe_dump({"image": image}))  # real newlines, ints, lists


def test_image_does_not_change_the_cache_key_but_is_in_the_summary():
    a = d.parse("apt: [git]")
    b = d.parse("image: images:ubuntu/24.04\napt: [git]")
    assert d.cache_key(a) == d.cache_key(b) is not None
    assert any("images:ubuntu/24.04" in l for l in d.summary(b))
    assert d.cache_key(d.parse("image: images:ubuntu/24.04")) is None  # nothing to bake


# --------------------------------------------------------------------------- #
# check: final gate when Claude exits (same entry shape as setup:)
# --------------------------------------------------------------------------- #
def test_check_parses_like_setup_and_is_independent_of_it():
    parsed = d.parse("check:\n  - {name: t, path: tools/final.sh, timeout: 60}\n"
                     "setup:\n  - {name: t, path: tools/setup.sh}\n")
    assert parsed["check"] == [{"name": "t", "path": "tools/final.sh",
                                "args": [], "env": {}, "timeout": 60}]
    assert [s["path"] for s in parsed["setup"]] == ["tools/setup.sh"]
    assert d.parse("apt: [git]")["check"] == []


@pytest.mark.parametrize("entry", [
    "{name: a, path: ../x.sh}", "{name: a, path: /abs.sh}",
    "{name: a, path: x.sh, timeout: 0}", "{name: a, path: x.sh, timeout: 0}",
    "{name: a, path: x.sh, env: {LD_PRELOAD: x}}",
])
def test_check_rejects_bad_entries_naming_the_section(entry):
    with pytest.raises(DepsError, match="check"):
        d.parse(f"check:\n  - {entry}\n")


def test_check_is_never_baked_and_is_summarised():
    assert d.cache_key(d.parse("check:\n  - {name: a, path: x.sh}\n")) is None
    parsed = d.parse("check:\n  - {name: a, path: tools/final.sh}\n")
    assert any("tools/final.sh" in l and "exit" in l for l in d.summary(parsed))


# --- unknown keys warn, known keys are still validated --------------------- #
def test_unknown_keys_warn_and_are_ignored():
    deps = d.parse("""
future_section: [1]
apt: [jq]
binaries:
  - {name: b, url: 'https://x/b', sha256: %s, newfield: 1}
setup:
  - {name: s, path: a.sh, shiny: true}
""" % SHA)
    assert deps["apt"] == ["jq"] and "future_section" not in deps
    assert "newfield" not in deps["binaries"][0]
    text = "\n".join(deps["warnings"])
    assert "future_section" in text and "newfield" in text and "shiny" in text
    assert d.parse("apt: [jq]")["warnings"] == []


def test_unknown_key_names_are_printed_safely():
    (w,) = d.parse(yaml.safe_dump({"\x1b[31m" + "x" * 100: 1}))["warnings"]
    assert "\x1b" not in w and "..." in w


def test_known_fields_are_still_validated_when_unknown_ones_are_present():
    with pytest.raises(DepsError, match="missing key"):
        d.parse("binaries: [{name: b, url: 'https://x/b', extra: 1}]")
    with pytest.raises(DepsError, match="invalid package"):
        d.parse("future: 1\napt: ['-o=evil']")


# --- downloads: ------------------------------------------------------------ #
def _file(**kw):
    e = {"name": "cfg", "dest": "~/.config/tool/cfg", "sha256": SHA,
         "url": "https://example.com/cfg"}
    e.update(kw)
    return e


def _parse_dl(*entries):
    return d.parse(yaml.safe_dump({"downloads": list(entries)}))["downloads"]


def test_downloads_defaults():
    (a,) = _parse_dl(_file())
    assert (a["mode"], a["on_error"], a["host_path"], a["url"]) == (
        "0644", "fail", None, "https://example.com/cfg")
    assert d.parse("downloads: []")["downloads"] == []
    assert d.parse("")["downloads"] == [] and d.parse("")["files"] == []


@pytest.mark.parametrize("entry", [
    {k: v for k, v in _file().items() if k != "sha256"},   # pin is required
    {k: v for k, v in _file().items() if k != "url"},      # no source
    _file(url="http://example.com/x"),                     # not https
    _file(dest="/etc/passwd"), _file(dest="x"), _file(dest="~/../etc/x"),
    _file(dest="~/a/./b"), _file(dest="~/"), _file(dest="~/a b"),
    _file(mode=644), _file(mode="4755"), _file(mode="0o644"),
    _file(on_error="ignore"), _file(on_error=True),
])
def test_downloads_reject_bad_entries(entry):
    with pytest.raises(DepsError):
        _parse_dl(entry)


@pytest.mark.parametrize("dest", [
    "~/.ssh/authorized_keys", "~/.claude/settings.json", "~/.claude.json",
    "~/.bashrc", "~/.profile", "~/.gitconfig", "~/.config/git/config",
    "~/.config/autostart/x.desktop", "~/.config/systemd/user/x.service",
    "~/.zshrc", "~/.tmux.conf", "~/.netrc"])
def test_downloads_deny_persistence_and_credential_destinations(dest):
    with pytest.raises(DepsError, match="not allowed"):
        _parse_dl(_file(dest=dest))


def test_downloads_allow_neighbours_of_denied_names():
    _parse_dl(_file(dest="~/.sshfoo"), _file(name="b", dest="~/.config/gitx/c"),
              _file(name="c", dest="~/.local/share/app/data"))


def test_downloads_duplicates_and_limits():
    with pytest.raises(DepsError, match="duplicate names"):
        _parse_dl(_file(), _file(dest="~/b"))
    with pytest.raises(DepsError, match="duplicate dests"):
        _parse_dl(_file(), _file(name="b"))
    with pytest.raises(DepsError, match="at most"):
        _parse_dl(*[_file(name=f"n{i}", dest=f"~/f{i}")
                    for i in range(d.MAX_FILES + 1)])


def test_files_are_part_of_the_cache_key_by_what_lands_on_disk():
    base = d.parse("apt: [jq]")
    f1 = {"dest": "~/a", "mode": "0644", "sha256": SHA, "url": "https://x/a",
          "host_path": None, "name": "a", "on_error": "fail"}
    key = lambda **kw: d.cache_key(dict(base, files=[dict(f1, **kw)]))  # noqa: E731
    assert d.cache_key(base) != key()                     # adding a file rebuilds
    assert key() == key(url="https://other/a", name="z")  # source/name don't count
    assert key() == key(url=None, host_path="/h/a")       # same bytes, same image
    assert key() != key(sha256="b" * 64)
    assert key() != key(mode="0600")
    assert key() != key(dest="~/b")
    # files alone are cacheable; file-less keys are unchanged by the new feature
    assert d.cache_key(dict(d.parse(""), files=[f1])) is not None
    assert d.cache_key(base) == d.cache_key(dict(base, files=[]))


def test_downloads_show_in_summary():
    assert any("~/.config/tool/cfg" in l
               for l in d.summary(d.parse(yaml.safe_dump({"downloads": [_file()]}))))
