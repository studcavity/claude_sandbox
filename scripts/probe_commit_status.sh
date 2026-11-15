#!/usr/bin/env bash
# probe_commit_status.sh - throwaway-repo experiment for docs/plans/05-sandbox-check-delta.md
#
# Answers, against real GitHub, whether the launcher can gate PR merges with a
# commit status posted from the host (no workflow in the repo):
#   1. can your gh login post a commit status?
#   2. does a ruleset accept a required status context that has never reported?
#   3. is a PR blocked without the status, and mergeable with it?
#   4. does a new commit on the PR branch lose the status (blocked again)?
#   5. does a `failure` status block, and does `success` then allow the merge?
#   6. break glass (docs/plans/README.md): does an override status unblock one
#      SHA only; does a *partial* ruleset PUT (`-f enforcement=disabled`) keep
#      the rules and open the gate; does re-enabling close it again; is
#      `evaluate` mode accepted on this plan?
#
# Run on a HOST where `gh` is logged in. It creates ONE new repo under your
# account (public by default: rulesets are not enforced on private repos of
# free personal accounts), touches nothing else, and deletes it on exit.
# Deleting needs the delete_repo scope: `gh auth refresh -s delete_repo`.
#
# Guard: this uses your GitHub credentials to create and delete a repo, so it
# must be asked for explicitly (--yes) and refuses to run inside a sandbox
# container, which has no gh login and must not be given one.
[ -f /etc/claude-sandbox ] && { echo "refusing: run this on your host, not inside a claude_sandbox container" >&2; exit 1; }
set -euo pipefail

CONTEXT="sandbox/check"
YES=0 KEEP=0 VIS=public OWNER=""
usage() {
  cat >&2 <<U
usage: $0 --yes [--visibility public|private] [--owner LOGIN] [--keep]
  --yes         required: acknowledges a repo will be created (and deleted)
  --visibility  default public (private needs a paid plan for rulesets to enforce)
  --owner       account to create the repo under (default: your gh login)
  --keep        don't delete the repo afterwards
U
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --keep) KEEP=1 ;;
    --visibility) VIS="${2:-}"; shift ;;
    --owner) OWNER="${2:-}"; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
  shift
done
[ "$YES" = 1 ] || usage
case "$VIS" in public|private) ;; *) usage ;; esac
command -v gh >/dev/null || { echo "gh is not installed" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not logged in: run 'gh auth login'" >&2; exit 1; }

RESULTS=()
note() { RESULTS+=("$1|$2|$3"); printf '  [%s] %s: %s\n' "$1" "$2" "$3"; }
FAILED=0
pass() { note PASS "$1" "${2:-}"; }
fail() { note FAIL "$1" "${2:-}"; FAILED=1; }
info() { note INFO "$1" "${2:-}"; }
skip() { note SKIP "$1" "${2:-}"; }

REPO="" CREATED=0
# shellcheck disable=SC2317  # invoked via trap
cleanup() {
  [ "$CREATED" = 1 ] || return 0
  if [ "$KEEP" = 1 ]; then echo "kept: https://github.com/$REPO"; return 0; fi
  if gh repo delete "$REPO" --yes >/dev/null 2>&1; then
    echo "deleted $REPO"
  else
    echo "COULD NOT DELETE $REPO. Run: gh auth refresh -s delete_repo && gh repo delete $REPO --yes" >&2
  fi
}
trap cleanup EXIT

scopes=$(gh api -i / 2>/dev/null | tr -d '\r' | awk -F': ' 'tolower($1)=="x-oauth-scopes"{print $2}')
echo "gh scopes: ${scopes:-<not reported (fine-grained token?)>}"
case ",$scopes," in *delete_repo*) ;; *) echo "note: no delete_repo scope; the repo will be left behind (command printed at the end)" ;; esac

OWNER="${OWNER:-$(gh api user --jq .login)}"
REPO="$OWNER/sandbox-status-probe-$(date +%s)"
echo "creating $VIS repo $REPO"
gh repo create "$REPO" "--$VIS" --add-readme >/dev/null
CREATED=1

B64() { printf '%s' "$1" | base64 | tr -d '\n'; }
BASE=$(gh api "repos/$REPO" --jq .default_branch)
BR="" FILE="" HEAD1="" PR="" FILE_SHA=""
new_pr() { # new_pr BRANCH: branch off the default branch, one commit, open a PR
  local base_sha out
  BR="$1" FILE="$1.txt"
  base_sha=$(gh api "repos/$REPO/git/ref/heads/$BASE" --jq .object.sha)
  gh api -X POST "repos/$REPO/git/refs" -f ref="refs/heads/$BR" -f sha="$base_sha" >/dev/null
  out=$(gh api -X PUT "repos/$REPO/contents/$FILE" -f message="probe: $BR" \
          -f content="$(B64 one)" -f branch="$BR")
  HEAD1=$(printf '%s' "$out" | jq -r .commit.sha)
  FILE_SHA=$(printf '%s' "$out" | jq -r .content.sha)
  # PULLS-POST
  PR=$(gh api -X POST "repos/$REPO/pulls" -f title="probe $BR" -f head="$BR" -f base="$BASE" --jq .number)
  echo "PR #$PR ($BR) head $HEAD1"
}
push_again() { # push_again: a second commit on the current branch -> new head
  local out
  out=$(gh api -X PUT "repos/$REPO/contents/$FILE" -f message="probe: $BR again" \
          -f content="$(B64 two)" -f sha="$FILE_SHA" -f branch="$BR")
  HEAD2=$(printf '%s' "$out" | jq -r .commit.sha)
}
HEAD2=""
new_pr probe

merge_state() { gh pr view "$PR" -R "$REPO" --json mergeStateStatus --jq .mergeStateStatus 2>/dev/null || echo ERROR; }
STATE=""
wait_for() { # wait_for WANT: poll until mergeStateStatus == WANT; sets STATE
  local _
  for _ in $(seq 1 "${POLL_TRIES:-15}"); do
    STATE=$(merge_state)
    [ "$STATE" = "$1" ] && return 0
    sleep "${POLL_SLEEP:-2}"
  done
  return 1
}
post_status() { # post_status SHA STATE
  gh api -X POST "repos/$REPO/statuses/$1" -f state="$2" -f context="$CONTEXT" \
     -f description="probe: $2" >/dev/null
}
try_merge() { # prints GitHub's message; returns 0 only if merged
  gh api -X PUT "repos/$REPO/pulls/$PR/merge" -f merge_method=squash 2>&1
}

echo "step 1: ruleset requiring '$CONTEXT' (never reported yet)"
ruleset=$(cat <<J
{"name":"probe-require-sandbox-check","target":"branch","enforcement":"active",
 "conditions":{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}},
 "bypass_actors":[],
 "rules":[{"type":"required_status_checks","parameters":{
   "strict_required_status_checks_policy":true,
   "required_status_checks":[{"context":"$CONTEXT"}]}}]}
J
)
RS_ID="" ERRF=$(mktemp)
if RS_ID=$(printf '%s' "$ruleset" | gh api -X POST "repos/$REPO/rulesets" --input - --jq .id 2>"$ERRF"); then
  pass "ruleset accepts a never-reported required context" "id $RS_ID"
  ENFORCED=1
else
  fail "ruleset accepts a never-reported required context" "$(head -c 300 "$ERRF")"
  ENFORCED=0
fi
rm -f "$ERRF"

if [ "$ENFORCED" = 1 ]; then
  echo "step 2: no status yet -> must be blocked"
  if wait_for BLOCKED; then pass "PR blocked without the status" "mergeStateStatus=$STATE"
  else fail "PR blocked without the status" "mergeStateStatus=$STATE (expected BLOCKED)"; fi
  if msg=$(try_merge); then
    fail "merge API refuses while status is missing" "it MERGED; ruleset is not enforcing"
    echo "PR merged unexpectedly; later steps are meaningless" >&2
    skip "remaining steps" "PR already merged"
    ENFORCED=0
  else
    pass "merge API refuses while status is missing" "$(printf '%s' "$msg" | head -c 200)"
  fi
else
  skip "blocking steps" "ruleset could not be created (private repo on a free plan?)"
fi

echo "step 3: post success from this gh login"
if post_status "$HEAD1" success; then
  pass "gh login can post a commit status" "scopes: ${scopes:-unknown}"
  who=$(gh api "repos/$REPO/commits/$HEAD1/statuses" --jq '.[0].creator | "\(.login) (type: \(.type))"' 2>/dev/null || echo "?")
  info "status creator" "$who"
else
  fail "gh login can post a commit status" "POST /statuses failed; check the repo:status scope"
  ENFORCED=0
fi

if [ "$ENFORCED" = 1 ]; then
  if wait_for CLEAN; then pass "PR mergeable once the status is success" "mergeStateStatus=$STATE"
  else fail "PR mergeable once the status is success" "mergeStateStatus=$STATE (expected CLEAN)"; fi

  echo "step 4: new commit on the branch -> status must be gone"
  push_again
  if wait_for BLOCKED; then pass "new commit loses the status" "head $HEAD1 -> $HEAD2, mergeStateStatus=$STATE"
  else fail "new commit loses the status" "mergeStateStatus=$STATE (expected BLOCKED)"; fi

  echo "step 5: failure blocks, then success merges"
  post_status "$HEAD2" failure
  if wait_for BLOCKED; then pass "a failure status blocks" "mergeStateStatus=$STATE"
  else fail "a failure status blocks" "mergeStateStatus=$STATE (expected BLOCKED)"; fi
  post_status "$HEAD2" success
  wait_for CLEAN || true
  if msg=$(try_merge); then pass "merge succeeds after success on the new head"
  else fail "merge succeeds after success on the new head" "$(printf '%s' "$msg" | head -c 200)"; fi
fi

if [ "$ENFORCED" = 1 ] && [ -n "$RS_ID" ]; then
  echo "step 6: break glass"
  rs_enf() { gh api "repos/$REPO/rulesets/$RS_ID" --jq .enforcement 2>/dev/null || echo ERROR; }
  rs_rules() { gh api "repos/$REPO/rulesets/$RS_ID" \
      --jq '[.rules[] | select(.type=="required_status_checks")] | length' 2>/dev/null || echo ERROR; }

  # Level 1: override one PR, one SHA
  new_pr bg-override
  if wait_for BLOCKED; then pass "level 1: new PR starts blocked" "mergeStateStatus=$STATE"
  else fail "level 1: new PR starts blocked" "mergeStateStatus=$STATE"; fi
  desc="OVERRIDDEN by $OWNER: probe false positive (reason recorded)"
  if gh api -X POST "repos/$REPO/statuses/$HEAD1" -f state=success -f context="$CONTEXT" \
        -f description="$desc" >/dev/null 2>&1; then
    pass "level 1: override status with a descriptive reason is accepted" "${#desc} chars"
  else
    fail "level 1: override status with a descriptive reason is accepted"
  fi
  if wait_for CLEAN; then pass "level 1: override makes the PR mergeable" "mergeStateStatus=$STATE"
  else fail "level 1: override makes the PR mergeable" "mergeStateStatus=$STATE"; fi
  push_again
  if wait_for BLOCKED; then pass "level 1: override does not survive a new commit" "mergeStateStatus=$STATE"
  else fail "level 1: override does not survive a new commit" "mergeStateStatus=$STATE"; fi

  # Level 2: suspend the ruleset with the README's partial PUT
  new_pr bg-suspend
  wait_for BLOCKED || true
  if err=$(gh api -X PUT "repos/$REPO/rulesets/$RS_ID" -f enforcement=evaluate 2>&1 >/dev/null); then
    info "evaluate mode" "accepted on this plan (README still recommends 'disabled')"
  else
    info "evaluate mode" "refused: $(printf '%s' "$err" | head -c 160) (consistent with the README's use of 'disabled')"
  fi
  gh api -X PUT "repos/$REPO/rulesets/$RS_ID" -f enforcement=active >/dev/null 2>&1 || true
  if gh api -X PUT "repos/$REPO/rulesets/$RS_ID" -f enforcement=disabled >/dev/null 2>&1; then
    pass "level 2: partial PUT enforcement=disabled accepted" "enforcement now $(rs_enf)"
  else
    fail "level 2: partial PUT enforcement=disabled accepted" "README runbook needs updating"
  fi
  n=$(rs_rules)
  if [ "$n" = 1 ]; then pass "level 2: partial PUT kept the rules" "required_status_checks rules: $n"
  else fail "level 2: partial PUT kept the rules" "rules left: $n; the README must say to re-send the full ruleset"; fi
  if msg=$(try_merge); then pass "level 2: merge succeeds while the ruleset is disabled"
  else fail "level 2: merge succeeds while the ruleset is disabled" "$(printf '%s' "$msg" | head -c 200)"; fi
  if gh api -X PUT "repos/$REPO/rulesets/$RS_ID" -f enforcement=active >/dev/null 2>&1 \
     && [ "$(rs_enf)" = active ]; then
    pass "level 2: partial PUT enforcement=active re-enables" "enforcement=$(rs_enf)"
  else
    fail "level 2: partial PUT enforcement=active re-enables" "enforcement=$(rs_enf)"
  fi
  if [ "$(rs_rules)" != 1 ]; then
    info "level 2 fallback" "restoring the full ruleset body"
    printf '%s' "$ruleset" | gh api -X PUT "repos/$REPO/rulesets/$RS_ID" --input - >/dev/null 2>&1 || true
  fi
  new_pr bg-after
  if wait_for BLOCKED && ! try_merge >/dev/null; then pass "gate is closed again after re-enabling" "mergeStateStatus=$STATE"
  else fail "gate is closed again after re-enabling" "mergeStateStatus=$STATE"; fi
fi

echo
echo "== summary for $REPO (gh $(gh --version | head -1)) =="
for r in "${RESULTS[@]}"; do IFS='|' read -r s n d <<<"$r"; printf '%-5s %s%s\n' "$s" "$n" "${d:+  -- $d}"; done
[ "$FAILED" = 0 ] && echo "ALL CHECKS PASSED" || echo "SOME CHECKS FAILED: paste this summary back"
exit "$FAILED"
