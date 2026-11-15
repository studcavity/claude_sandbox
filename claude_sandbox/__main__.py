"""The launcher's entry point: `./run.sh` execs this file, and
`python -m claude_sandbox` runs it too.

Started as a script (run.sh), sys.path[0] is this package's own directory;
that is replaced with the repo root so `claude_sandbox` imports and no module
here can shadow a stdlib one. Then, before anything third-party is imported,
the stdlib-only bootstrap makes sure a venv with requirements.txt exists and
re-execs this same file under it (a no-op when already inside one).
"""
import os
import sys

if __name__ == "__main__":
    _pkg = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_pkg)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _pkg]
    if _root not in sys.path:
        sys.path.insert(0, _root)

    from claude_sandbox import bootstrap
    bootstrap.ensure_venv(_root)

    from claude_sandbox import cli
    sys.exit(cli.main())
