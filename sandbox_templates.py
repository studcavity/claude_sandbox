"""sandbox_templates.py - the wording of the issues and PRs claude_sandbox files.

The text lives in templates/*.md (string.Template, `$$` for a literal dollar
sign) so it can change without touching code. What must NOT be editable by
accident is checked on load:
  * every placeholder the code fills in is present (a template that drops
    `$caveats` would silently stop showing reviewers the commits' caveats);
  * no placeholder the code doesn't fill in (a typo fails now, not at 2am);
  * the text still says the content is "untrusted".
The ledger marker of an issue is NOT in the template: code appends it as
the last line, so no wording change can break deduplication.
Stdlib only. Values passed in are already sanitised by the caller.
"""
import os
import string

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

REQUIRED = {
    "pr.md": {"session", "branch", "caveats", "subjects", "more"},
    "issue.md": {"commit", "item", "body"},
}


class TemplateError(ValueError):
    pass


def identifiers(text):
    """Placeholders in `text`; raises TemplateError on a malformed one."""
    found = set()
    for m in string.Template.pattern.finditer(text):
        if m.group("invalid") is not None:
            raise TemplateError("a lone `$` in the template (write `$$`)")
        name = m.group("named") or m.group("braced")
        if name:
            found.add(name)
    return found


def load(name, directory=None):
    """The validated template text for `name` (a single trailing newline is
    dropped, so files can end the way editors like them to)."""
    if name not in REQUIRED:
        raise TemplateError(f"unknown template {name!r}")
    path = os.path.join(directory or TEMPLATE_DIR, name)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise TemplateError(f"{name}: {e.strerror or e}") from None
    got, want = identifiers(text), REQUIRED[name]
    if want - got:
        raise TemplateError(f"{name} is missing {', '.join('$' + n for n in sorted(want - got))}")
    if got - want:
        raise TemplateError(f"{name} uses unknown {', '.join('$' + n for n in sorted(got - want))}")
    if "untrusted" not in text.lower():
        raise TemplateError(f"{name} must say the content is untrusted")
    return text[:-1] if text.endswith("\n") else text


def render(name, directory=None, **values):
    return string.Template(load(name, directory)).substitute(**values)
