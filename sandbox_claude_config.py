"""sandbox_claude_config.py - pre-answer Claude Code's first-run questions.

A fresh container makes Claude Code ask for a theme, a login method, whether
to trust the folder and whether to use ANTHROPIC_API_KEY. We know the answers,
so we seed them (each key was checked against Claude Code 2.1.325 by watching
which dialog disappears):

  ~/.claude.json   hasCompletedOnboarding            skips welcome/login picker
                   customApiKeyResponses.approved    "use this API key?" dialog
                   projects[dir].hasTrustDialogAccepted   "do you trust this folder?"
  ~/.claude/settings.json   theme                    the theme picker

Trusting a folder lets its project settings, hooks and MCP servers run, so it
is granted only for a repo the launching user owns (`trust_decision`), never
for someone else's code.

Pure functions plus a script to run inside the container; no I/O here.
Nothing existing fit: these keys are Claude Code internals, not a public API.
"""
import json

# "Dark mode (colorblind-friendly)" in the theme picker.
THEME = "dark-daltonized"

# Claude Code remembers an approved key by its last 20 characters.
API_KEY_TAIL = 20

# Run in the container as `python3 -c`; reads {relative path: seed} on stdin
# and deep-merges each seed into the file (dicts merge, anything else is
# replaced), so keys Claude Code already wrote survive. Atomic replace.
MERGE_SCRIPT = """\
import json, os, sys
def merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            merge(a[k], v)
        else:
            a[k] = v
    return a
for rel, seed in json.load(sys.stdin).items():
    path = os.path.join(os.path.expanduser("~"), rel)
    try:
        with open(path) as f:
            cur = json.load(f)
    except FileNotFoundError:
        cur = {}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(merge(cur, seed), f, indent=2)
    os.replace(path + ".tmp", path)
"""


def trust_decision(owner, login):
    """(trusted, note): trust the folder only if `owner` (the repo's owner as
    GitHub reports it, so a renamed or transferred repo can't pass as ours)
    is the authenticated user `login`. Organisations don't match; the dialog
    just appears for them."""
    if owner and login and owner.lower() == login.lower():
        return True, None
    return False, (f"repo is owned by {owner!r}, not {login!r}: not "
                   "pre-trusting the folder, Claude Code will ask")


def claude_state(workdir, secrets, trusted, theme=THEME):
    """{relative path: seed} for MERGE_SCRIPT.

    Onboarding and the API-key approval are only seeded when we pass
    credentials: without them the user must still see the login flow.
    """
    config: dict = {}
    if secrets:
        config["hasCompletedOnboarding"] = True
    key = secrets.get("ANTHROPIC_API_KEY")
    if key:
        config["customApiKeyResponses"] = {
            "approved": [key[-API_KEY_TAIL:]], "rejected": []}
    if trusted:
        config["projects"] = {workdir: {"hasTrustDialogAccepted": True}}
    return {".claude.json": config, ".claude/settings.json": {"theme": theme}}


def merge_input(state):
    return json.dumps(state)
