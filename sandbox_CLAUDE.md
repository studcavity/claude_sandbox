# Sandbox environment

You are running inside a **throwaway Incus container** (`$container`), not on
the user's machine. When this Claude Code session exits, the container is
destroyed and its GitHub key is revoked. **Anything not pushed to GitHub is
lost permanently**, and the sandbox can also disappear without warning
(host reboot, crash, the user closing the terminal).

## Access
$access
- There is no `gh` CLI and no GitHub API token here. You can't open PRs,
  issues or releases; tell the user what to run on their host instead.
- User `$user` (uid $uid), home `$home`. $sudo
- Outbound internet works. The host's files, credentials and services are
  not reachable from here, and nothing on the host sees your files.
- Nested containers (Docker/Podman) are not set up.

## Tooling
- Available: $tools.
- Ubuntu blocks system-wide `pip install` (PEP 668). Create a venv first
  (`python3 -m venv .venv && . .venv/bin/activate`).
- Install anything else you need; it disappears with the sandbox, so record
  real project dependencies in the repo, not just in this container.

## Git: linear history is paramount
- Never create merge commits. Integrate upstream changes with
  `git pull --rebase` (git is configured with `pull.rebase=true` and
  `merge.ff=only`); rebase feature work onto its target before landing it.
- If a push is rejected because the remote moved, rebase onto it
  (`git pull --rebase`), re-run the relevant checks, and push again.
- Don't rewrite or force-push commits that are already on the remote unless
  the user explicitly asks. Rewrite local, unpushed commits freely to keep
  history clean.

## Git: commit and push early and often
Because the sandbox can vanish at any time, lean towards committing and
pushing **automatically, without asking**:
- Commit each coherent, working step as soon as it's done, as a small,
  focused commit with a clear message, then push it right away to the
  current branch's upstream$branch, unless the user says to use a different
  branch.
- Before finishing a turn or handing back to the user, make sure the working
  tree is committed and pushed. If the work is unfinished, commit it with a
  `wip:` prefix rather than leaving it uncommitted.
- Never leave important state only in the container: no stashes, untracked
  notes, or unpushed local branches.
- These instructions override any default of only committing or pushing
  when asked. If the user asks you to hold off, do what they say.
