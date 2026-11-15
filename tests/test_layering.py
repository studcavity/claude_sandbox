"""The package's import layering (spec 09): lower layers never import upward.

Checked on every import statement, function-level ones too: a lazy import is
still a dependency. `portable/` has its own, stricter test (stdlib only) in
test_claude_sandbox.py.
"""
import ast
import glob
import os

import pytest

LEAVES = {"paths", "timing", "util", "portable"}
# group -> the groups it may import (besides itself). Anything not listed here
# is free to import anything below it; these are the ones with a boundary.
ALLOWED = {
    "container": LEAVES,                              # incus never learns about GitHub
    "github": LEAVES | {"container", "sessions"},     # the host-side GitHub layer
    "backends": LEAVES | {"container", "github", "sessions", "provenance",
                          "claude_config", "backends"},
}
# Only the entry points may import the CLI and the lifecycle.
TOP = {"cli", "sandbox", "options", "__main__"}


def imports(path):
    """Top-level names under claude_sandbox that `path` imports."""
    found = set()
    for n in ast.walk(ast.parse(open(path).read())):
        if isinstance(n, ast.ImportFrom) and n.module and \
                n.module.split(".")[0] == "claude_sandbox":
            parts = n.module.split(".")[1:]
            found.update(parts[:1] or [a.name for a in n.names])
        elif isinstance(n, ast.Import):
            found.update(a.name.split(".")[1] for a in n.names
                         if a.name.startswith("claude_sandbox."))
    return found


def modules(repo_root):
    base = os.path.join(repo_root, "claude_sandbox")
    for f in sorted(glob.glob(os.path.join(base, "**", "*.py"), recursive=True)):
        rel = os.path.relpath(f, base)
        yield rel, rel.split(os.sep)[0].removesuffix(".py"), f


@pytest.mark.parametrize("group", sorted(ALLOWED))
def test_lower_layers_import_only_what_they_may(repo_root, group):
    for rel, g, path in modules(repo_root):
        if g != group:
            continue
        bad = imports(path) - ALLOWED[group] - {group}
        assert not bad, f"{rel} imports {sorted(bad)}"


def test_only_the_entry_points_import_the_cli_and_the_lifecycle(repo_root):
    for rel, g, path in modules(repo_root):
        if g in TOP:
            continue
        bad = imports(path) & TOP
        assert not bad, f"{rel} imports {sorted(bad)}"
