"""portable/todo.py - the follow-up request format: one file under `todo/`.

A session that wants a follow-up issue filed adds `todo/<name>.md` to a
commit. That file IS the request: its first heading is the issue's title, the
whole file is the issue text, and the path is its identity. There is no second
place to keep in step (this used to be a JSON trailer plus a brief).

Questions the format has to answer, and what it does:
  * Reuse of a path. A request is a *commit that adds the file*, so the
    filer keys on (path, adding commit). If `todo/fix-ci.md` is filed, deleted,
    and later added again for a different problem, the second add is a new
    request.
  * Edits after filing are ignored (only an add makes a request). The issue
    links to the file at the commit it was filed from.
  * Deleting a file never closes an issue: closing issues under the user's
    name from session-written commits would be a new power. Deleting it
    *before* merge withdraws the request, because only files that reach the
    default branch are filed.
  * Names are `[a-z0-9][a-z0-9-]*`, so a path is safe in URLs, labels and
    markers with no escaping. Two sessions choosing the same name conflict
    when the second rebases, which is the behaviour we want.

Stdlib only: `sandbox_check.py` imports this and runs in CI and in the sandbox.
"""
import re

DIR = "todo/"
NAME_RE = re.compile(r"^todo/([a-z0-9][a-z0-9-]{0,79})\.md$")
MAX_BYTES = 20000   # a brief longer than this is refused by CI and the filer
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(\S.*?)[ \t#]*$")


def is_candidate(path):
    """A markdown file under todo/ (whatever its name): the checker applies
    the format to these, and only these."""
    return path.startswith(DIR) and path.lower().endswith(".md")


def request_name(path):
    """`todo/fix-ci.md` -> `fix-ci`; None if the path isn't a valid request."""
    m = NAME_RE.match(path)
    return m.group(1) if m else None


def title(text):
    """The text of the first Markdown heading, or None."""
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            return m.group(1)
    return None


def problems(path, size, text=None):
    """Everything wrong with one `todo/` file -> [str]. `size` is its bytes;
    `text` is its content (not needed when it is already too large)."""
    out = []
    if not request_name(path):
        out.append(f"{path}: a follow-up request must be todo/<name>.md with "
                   "<name> made of lowercase letters, digits and `-` "
                   "(at most 80 characters, no subdirectories)")
    if size > MAX_BYTES:
        out.append(f"{path}: {size} bytes is over the {MAX_BYTES}-byte limit "
                   "for a follow-up request")
    elif text is not None and not title(text):
        out.append(f"{path}: no heading; the first heading becomes the "
                   "issue title")
    return out
