"""shell.py - the shell snippets the launcher runs in the container (marker, git config, secrets, session)."""

import shlex
from claude_sandbox.container.incus import HOME


SANDBOX_MARKER = "/etc/claude-sandbox"  # scripts test for this to gate hazards


SECRETS_FILE = f"{HOME}/.sandbox_secrets"  # inside the container


def marker_script(name, path=None):
    """Shell that writes the sandbox marker file (repo scripts test for it to
    refuse to run anywhere else). `name` is quoted; the path is ours."""
    path = path or SANDBOX_MARKER
    return (f"printf '%s\\n' {shlex.quote(name)} > {shlex.quote(path)} && "
            f"chmod 0644 {shlex.quote(path)}")


def git_config_script(git_name=None, git_email=None, github_ssh=True):
    """The `git config --global` calls a session needs, as one shell script:
    https->ssh rewrite for GitHub (the deploy key is ssh; skipped when there is
    no key, so public https clones still work), optional identity, and the
    linear-history settings. Values are quoted."""
    cmds = (['git config --global url."git@github.com:".insteadOf "https://github.com/"']
            if github_ssh else [])
    if git_name:
        cmds.append("git config --global user.name " + shlex.quote(git_name))
    if git_email:
        cmds.append("git config --global user.email " + shlex.quote(git_email))
    cmds += ["git config --global pull.rebase true",       # linear history:
             "git config --global rebase.autoStash true",  # rebase on pull,
             "git config --global merge.ff only",          # no merge commits,
             "git config --global push.autoSetupRemote true"]
    return " && ".join(cmds)


CLAUDE_INSTALL = "curl -fsSL https://claude.ai/install.sh | bash"


TMUX_CONF = """\
set -g mouse on
set -g history-limit 100000
set -g default-terminal tmux-256color
set -as terminal-features ',*:RGB'
set -g status off
"""


def secrets_script(secrets):
    """Shell `export` lines for secrets (sourced in the container)."""
    return "".join(f"export {k}={shlex.quote(v)}\n" for k, v in secrets.items())


def session_script(workdir, load_secrets, shell=False, model="default",
                   tmux=False, resume_prompt=None):
    """The bash -lc script that starts the interactive session. With
    `resume_prompt`, it continues the container's last conversation (the only
    one there is) and opens with that prompt."""
    prog = "bash -l" if shell else "claude"
    if not shell and model.lower() != "default":
        prog += f" --model {shlex.quote(model)}"
    if not shell and resume_prompt is not None:
        prog += f" --continue {shlex.quote(resume_prompt)}"
    if tmux:
        # tmux sets its own TERM/TERM_PROGRAM, giving Claude Code a
        # well-known terminal; scroll with the mouse or prefix-[ .
        prog = f"tmux new-session -s claude {shlex.quote(prog)}"
    return f"{load_secrets}cd {shlex.quote(workdir)} && exec {prog}"


def setup_argv(s):
    """argv for one validated `setup:` entry: bounded by `timeout` (killed
    10s later if it ignores SIGTERM), run as a login shell script so the
    session's PATH applies. Nothing is interpolated into a shell string."""
    return ["timeout", "--kill-after=10", str(s["timeout"]),
            "bash", "-l", "./" + s["path"], *s["args"]]
