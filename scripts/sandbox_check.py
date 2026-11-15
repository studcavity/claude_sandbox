"""sandbox_check.py - the commit checker's command line (CI, and the sandbox's
`sandbox-check`). The logic is claude_sandbox/portable/check.py; this file only
finds the package. It runs from a checkout's `scripts/` directory (CI runs the
trusted base branch's copy with `python -E -s`) and from the same layout
inside the sandbox, so the repo root is always the parent of this directory.

    python scripts/sandbox_check.py BASE HEAD [--strict] [--fresh]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude_sandbox.portable import check  # noqa: E402

if __name__ == "__main__":
    sys.exit(check.main())
