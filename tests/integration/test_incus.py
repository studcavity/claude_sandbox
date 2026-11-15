"""Opt-in tests against a REAL container. Skipped unless CS_INTEGRATION=1 and
incus (or lxc) is installed. Run with `./smoke.sh` or
`CS_INTEGRATION=1 python -m pytest -m incus`.

CS_INTEGRATION_IMAGE overrides the image (default images:alpine/edge)."""
import os
import shutil
import uuid

import pytest

from sandbox_containers import Container

pytestmark = pytest.mark.incus

CLI = next((c for c in ("incus", "lxc") if shutil.which(c)), None)
if os.environ.get("CS_INTEGRATION") != "1" or CLI is None:
    pytest.skip("set CS_INTEGRATION=1 and install incus to run",
                allow_module_level=True)


@pytest.fixture(scope="module")
def box():
    c = Container(CLI, f"cs-it-{uuid.uuid4().hex[:8]}")
    c.launch(os.environ.get("CS_INTEGRATION_IMAGE", "images:alpine/edge"),
             ephemeral=True)
    try:
        yield c
    finally:
        c.delete()


def test_put_file_contents_and_mode(box):
    box.put_file("/tmp/it-x", b"x", "0755")
    out = box.root_argv(["sh", "-c", "cat /tmp/it-x; stat -c %a /tmp/it-x"])
    assert out.stdout.decode().split() == ["x", "755"]
    assert box.root_argv(["ls", "/tmp"]).stdout.decode().count("it-x.tmp") == 0


def test_put_file_binary_and_odd_path(box):
    data = bytes(range(256)) * 100
    box.put_file("/tmp/it y's file", data, "0600")
    got = box.root_argv(["cat", "/tmp/it y's file"]).stdout
    assert got == data


def test_root_argv_passes_special_chars_verbatim(box):
    arg = "a b'\"$(id);`x`"
    assert box.root_argv(["printf", "%s", arg]).stdout.decode() == arg


def test_self_check(box):
    box.self_check()
