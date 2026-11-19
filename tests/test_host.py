"""Tests for host: host-side requests/approval (no containers)."""
import os

import pytest

from claude_sandbox.container import deps
from claude_sandbox.container import host as sh
GOOD = {"name": "probe", "vendor_id": "2E8A", "product_id": "000C",
        "description": "Pico debug probe (CMSIS-DAP), SWD"}


def _doc(**over):
    d = dict(GOOD, **over)
    first, *rest = d.items()
    return "usb:\n" + f"  - {first[0]}: {first[1]!r}\n" + "".join(
        f"    {k}: {v!r}\n" for k, v in rest)


def test_parse_normalises_and_defaults():
    (u,) = sh.parse(_doc())["usb"]
    assert (u["vendor_id"], u["product_id"], u["tools"]) == ("2e8a", "000c", [])


@pytest.mark.parametrize("over", [
    {"vendor_id": "2e8"}, {"vendor_id": "2e8a1"}, {"product_id": "zzzz"},
    {"name": "-x"}, {"name": "a b"},
    {"description": "line\nbreak"}, {"description": "x" * 201},
    {"description": "```yaml"}, {"description": "$HOME"},
    {"description": "# Heading"}, {"description": ""},
    {"tools": "pyocd"}, {"tools": ["a b"]}, {"tools": ["a"] * 9},
])
def test_parse_rejects(over):
    with pytest.raises(deps.DepsError):
        sh.parse(_doc(**over))


def test_unknown_keys_warn_and_known_ones_still_validate():
    cfg = sh.parse("apt: [git]\n" + "usb:\n  - {name: a, vendor_id: '2e8a', "
                   "product_id: '0001', description: d, extra: x}\n")
    assert cfg["usb"][0]["name"] == "a" and "extra" not in cfg["usb"][0]
    assert any("apt" in w for w in cfg["warnings"]) and any(
        "extra" in w for w in cfg["warnings"])
    with pytest.raises(deps.DepsError):
        sh.parse("apt: [git]\nusb: [{name: a, vendor_id: zz}]")


SHA = "a" * 64


def test_host_files_section():
    cfg = sh.parse("files:\n  - {name: c, dest: '~/.config/t/c', "
                   f"host_path: '~/dotfiles/c', sha256: {SHA}, "
                   "mode: '0600', on_error: skip}\n")
    (f,) = cfg["files"]
    assert (f["host_path"], f["url"], f["mode"], f["on_error"]) == (
        "~/dotfiles/c", None, "0600", "skip")
    assert sh.parse("")["files"] == []
    for bad in (f"{{name: c, dest: '~/c', url: 'https://x/c', sha256: {SHA}}}",  # wrong source
                f"{{name: c, dest: '~/.ssh/k', host_path: /h/k, sha256: {SHA}}}",
                "{name: c, dest: '~/c', host_path: /h/k}"):                  # no pin
        with pytest.raises(deps.DepsError):
            sh.parse(f"files: [{bad}]")


def test_parse_rejects_dupes_and_too_many():
    entry = "  - {name: %s, vendor_id: '2e8a', product_id: '%s', description: d}\n"
    with pytest.raises(deps.DepsError, match="names"):
        sh.parse("usb:\n" + entry % ("a", "0001") + entry % ("a", "0002"))
    with pytest.raises(deps.DepsError, match="vid:pid"):
        sh.parse("usb:\n" + entry % ("a", "0001") + entry % ("b", "0001"))
    with pytest.raises(deps.DepsError, match="at most"):
        sh.parse("usb:\n" + "".join(entry % (f"n{i}", f"{i:04x}")
                                    for i in range(9)))


def test_deps_file_rejects_usb_and_host_file_never_touches_cache_key():
    """usb: lives only in the host file, so it cannot reach the image cache."""
    parsed = deps.parse("usb: [{name: a}]")   # unknown here: warned, ignored
    assert "usb" not in parsed and any("usb" in w for w in parsed["warnings"])
    base = deps.parse("apt: [git]")
    import inspect
    from claude_sandbox.container import images as images
    assert "container import host" not in inspect.getsource(images)
    assert deps.cache_key(base)


@pytest.mark.parametrize("spec", ["2e8a:000c", "2E8A:000C"])
def test_parse_usb_spec(spec):
    assert sh.parse_usb_spec(spec) == ("2e8a", "000c")


def test_allowlist_parse_and_fail_closed():
    got = sh.parse_allowlist("# c\n2e8a:000c\n\n1234:ABCD Me/Repo  # x\n")
    assert got == {("2e8a", "000c", None), ("1234", "abcd", "me/repo")}
    for bad in ("2e8a", "2e8a:000c a b", "*:*"):
        with pytest.raises(ValueError):
            sh.parse_allowlist(bad)


def test_load_allowlist_permissions(tmp_path):
    assert sh.load_allowlist(str(tmp_path / "none")) == set()
    f = tmp_path / "a"
    f.write_text("2e8a:000c\n")
    os.chmod(f, 0o600)
    assert sh.load_allowlist(str(f)) == {("2e8a", "000c", None)}
    os.chmod(f, 0o666)
    with pytest.raises(ValueError, match="writable"):
        sh.load_allowlist(str(f))


REQ = sh.parse(_doc())["usb"]


def _resolve(flags=(), allow=(), repo="me/p", ask=None):
    return sh.resolve_usb(REQ, list(flags), set(allow), repo, ask)


def test_resolve_matrix():
    assert _resolve()[0]["approved"] is False                 # request only
    assert _resolve(ask=lambda r: True)[0]["approved"] is True
    assert _resolve(ask=lambda r: False)[0]["approved"] is False
    assert _resolve(flags=["2e8a:000c"])[0]["approved"] is True
    assert _resolve(allow=[("2e8a", "000c", None)])[0]["approved"] is True
    assert _resolve(allow=[("2e8a", "000c", "me/p")])[0]["approved"] is True
    assert _resolve(allow=[("2e8a", "000c", "me/other")])[0]["approved"] is False
    assert _resolve(allow=[("2e8a", "0003", None)])[0]["approved"] is False


def test_resolve_flag_wins_but_keeps_repo_text_and_does_not_prompt():
    (d,) = _resolve(flags=["2e8a:000c"],
                    ask=lambda r: pytest.fail("must not prompt"))
    assert d["source"] == "flag" and d["name"] == "probe"
    assert d["description"].startswith("Pico")


def test_display_text_strips_control_chars():
    assert sh.display_text("a\x1b[31mb\n") == "a?[31mb?"


def test_mounts_parse_with_defaults_and_validation():
    doc = sh.parse("mounts:\n  - {source: ~/data, description: Ref data}\n"
                     "  - {source: /srv/x, path: /mnt/x, mode: rw, "
                     "description: Scratch}\n")
    assert doc["usb"] == []
    assert doc["mounts"][0] == {"source": "~/data", "path": None, "mode": "ro",
                                "description": "Ref data"}
    assert doc["mounts"][1]["mode"] == "rw"


@pytest.mark.parametrize("entry", [
    "{source: relative/dir, description: d}",           # not absolute or ~
    "{source: '/a:b', description: d}",                 # delimiter
    "{source: /a, mode: wr, description: d}",           # bad mode
    "{source: /a, path: relative, description: d}",
    "{source: /a, description: 'has `tick`'}",          # markup in text
    "{source: /a}",                                      # no description
])
def test_bad_mount_entries_are_rejected(entry):
    with pytest.raises(deps.DepsError):
        sh.parse(f"mounts:\n  - {entry}\n")


def test_too_many_mounts_rejected():
    many = "".join(f"  - {{source: /a{i}, description: d}}\n" for i in range(9))
    with pytest.raises(deps.DepsError):
        sh.parse("mounts:\n" + many)


@pytest.mark.parametrize("raw, want", [
    ("", None), ("nesting: false", None), ("nesting: true", {"description": ""}),
    ("nesting: {description: Tests need it}", {"description": "Tests need it"}),
    ("nesting: {}", {"description": ""}),
])
def test_nesting_request(raw, want):
    assert sh.parse(raw)["nesting"] == want


@pytest.mark.parametrize("raw", [
    "nesting: yes please", "nesting: {description: 'has `tick`'}",
    "nesting: [1]", "nesting: {description: 5}"])
def test_bad_nesting_request_is_rejected(raw):
    with pytest.raises(deps.DepsError):
        sh.parse(raw)


def test_unknown_nesting_key_warns():
    cfg = sh.parse("nesting: {description: d, privileged: true}")
    assert cfg["nesting"] == {"description": "d"} and cfg["warnings"]


def test_this_repos_host_file_requests_nesting_for_the_smoke_tests(repo_root):
    with open(os.path.join(repo_root, sh.HOST_FILE), "rb") as f:
        cfg = sh.parse(f.read())
    assert cfg["nesting"] and cfg["nesting"]["description"]
    assert cfg["warnings"] == [] and cfg["usb"] == cfg["mounts"] == []
    with open(os.path.join(repo_root, "scripts", "final-check.sh")) as f:
        assert "-m incus tests/integration" in f.read()
