"""paths.py - where things live in a checkout. Stdlib only.

One place for every location the code derives from its own file, so moving a
directory is a one-line change here instead of a hunt for `__file__`.
"""
import os

PACKAGE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PACKAGE)
MAIN_SCRIPT = os.path.join(PACKAGE, "__main__.py")
DATA = os.path.join(PACKAGE, "data")  # session CLAUDE.md, deps reference, templates/
RULESETS = os.path.join(ROOT, "rulesets")
SCRIPTS = os.path.join(ROOT, "scripts")
SESSION_DOCS = os.path.join(ROOT, "docs", "session")  # source of the session's embedded docs
