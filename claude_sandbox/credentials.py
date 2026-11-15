"""credentials.py - the Claude OAuth token file and the secrets piped into a session."""

import os


# --------------------------------------------------------------------------- #
# pure helpers (no I/O; unit-tested)
# --------------------------------------------------------------------------- #
def read_token_file(path):
    try:
        with open(path) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_token_file(path, token):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, path)


def claude_secrets(environ, token_file):
    """Claude credentials to pass into the sandbox, from env or token file."""
    secrets = {}
    token = environ.get("CLAUDE_CODE_OAUTH_TOKEN") or read_token_file(token_file)
    if token:
        secrets["CLAUDE_CODE_OAUTH_TOKEN"] = token
    if environ.get("ANTHROPIC_API_KEY"):
        secrets["ANTHROPIC_API_KEY"] = environ["ANTHROPIC_API_KEY"]
    return secrets
