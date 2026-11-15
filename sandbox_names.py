"""sandbox_names.py - the few naming rules shared by the launcher and the
stand-alone checker.

Stdlib only, on purpose: `sandbox_check.py` runs in CI and (planned) inside the
sandbox, where the launcher's third-party dependencies (invoke, via the gh
wrapper) are not installed. Anything the checker needs to agree on with the
launcher belongs here, not in `sandbox_sessions`, which imports `sandbox_github`.
"""
import re

# Session names: a sandbox's identity, used in branch names and trailers. Same
# alphabet as an incus instance name; <=40 so branch names stay short.
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,39}$")
# Every session's branches live under <BRANCH_ROOT>/<session>/.
BRANCH_ROOT = "sbx"
