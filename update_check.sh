#!/bin/bash
# Sourced by run.sh. Defines check_for_updates, which tells the user when this
# checkout is behind its upstream and, on a terminal, offers to `git pull`.

# check_for_updates DIR SELF [ARGS...]
#   DIR   the checkout to check
#   SELF/ARGS  the command to re-run after a successful pull (so the new
#         scripts are the ones that run)
# Quietly does nothing when offline or without an upstream. Never blocks the
# launch: a declined or failed pull just continues with the current version.
check_for_updates() {
  local dir="$1" up behind
  shift
  [ -z "${RUN_SH_SKIP_UPDATE_CHECK:-}" ] || return 0
  up="$(git -C "${dir}" rev-parse --abbrev-ref '@{u}' 2>/dev/null)" || return 0
  GIT_TERMINAL_PROMPT=0 timeout 10 git -C "${dir}" fetch --quiet 2>/dev/null || {
    echo "[run.sh] note: couldn't check for updates (git fetch failed)" >&2
    return 0
  }
  behind="$(git -C "${dir}" rev-list --count 'HEAD..@{u}' 2>/dev/null)" || return 0
  [ "${behind}" -gt 0 ] || return 0

  echo "[run.sh] NOTICE: this checkout is ${behind} commit(s) behind ${up}." >&2
  git -C "${dir}" log --oneline 'HEAD..@{u}' | sed 's/^/[run.sh]   /' >&2
  if [ ! -t 0 ]; then
    echo "[run.sh] run 'git -C ${dir} pull' and re-run to use the latest version." >&2
    return 0
  fi
  local answer=""
  read -r -p "[run.sh] git pull now? [y/N] " answer || true
  case "${answer}" in
    y | Y | yes | YES) ;;
    *) echo "[run.sh] continuing with the current version." >&2; return 0 ;;
  esac
  # --ff-only: never create a merge commit or touch local work; if it can't
  # fast-forward (diverged, dirty overlap) say so and carry on.
  if git -C "${dir}" pull --ff-only >&2; then
    echo "[run.sh] updated; restarting." >&2
    export RUN_SH_SKIP_UPDATE_CHECK=1
    exec "$@"
  fi
  echo "[run.sh] pull failed; continuing with the current version." >&2
}
