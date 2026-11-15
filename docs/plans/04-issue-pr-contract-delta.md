# 4. Issue and PR content contract: delta on what exists

**What exists.** Issues: `sandbox_actions._issue` builds a fixed body
(provenance line, sanitised session text, "untrusted" footer, hidden marker).
PRs: `sandbox_land.pr_text(session, branch, subjects)` builds the title and body
from commit subjects ("Opened by claude_sandbox when session ... ended"),
notes stacking, and holds PRs touching privileged files. The sandbox CLAUDE.md
tells sessions what trailers and briefs to write. **No labels, no required
sections, no templates, and the two builders share no code.**

Goal unchanged: every issue and PR a session causes has the same labels,
structure and safety, enforced by deterministic launcher code (plans 3 and 5),
not by trusting the session.

## Delta

### 4a. PR body carries the commit record
Extend `pr_text` to also extract from the branch's commit messages, and list
in the body: every `Verified:` line, every `Not verified:` line, every
`Follow-up:` line and every `Sandbox-Action` issue (title + id), each tagged
with its short sha. A reviewer then sees the caveats without reading every
commit. Pure function over messages; sanitise through plan 3a's module. This
is also the data plan 5's PR-level checks compare against.

### 4b. Labels on PRs
Add `sandbox`, `session:<id>`, plus `needs-human` when the PR is held for
privileged files and `check-failed` when plan 5's check reported errors. One
label table shared with plan 3c; create-if-missing.

### 4c. Templates
- Issue and PR text rendered from two small `string.Template` files in this
  repo (`templates/issue.md`, `templates/pr.md`) so wording changes without code
  changes; both builders use the same loader and the same required-section
  list. Keep `$$` escaping rules from `sandbox_CLAUDE.md`.
- Optionally offer, for repos the user owns, to add self-contained
  `.github/pull_request_template.md` / `ISSUE_TEMPLATE/` files rendered by the
  launcher (never a reference back to this repo), through a normal PR held for
  review because it touches `.github/`. Anything beyond that is a requirement
  stated in the sandbox CLAUDE.md.

### 4d. Let sessions lint before they exit
A session can't run `sandbox_check.py` today (not in the container, and it
isn't stdlib-only: it imports `sandbox_sessions`, which pulls in `invoke`
through `sandbox_github`). Make the checker's import graph stdlib-only (move
`BRANCH_ROOT` out of `sandbox_sessions`, or import it lazily), then have the
launcher copy `sandbox_check.py`, `sandbox_actions.py` and
`sandbox_untrusted.py` into the container with `put_file` and add a
`sandbox-check` wrapper. The CLAUDE.md tells the session to run it on its own
branch before finishing, so a malformed trailer is fixed while it still can be.
Honest limit, stated there: the session has `sudo` and can bypass its own
copy; the real enforcement is host-side (plan 5).

### 4e. Not doing
- In-container `sandbox-issue`/`sandbox-pr` scripts: they only made sense with
  a token or broker. Trailers replace them.
- A `pr` action type: PRs are already opened per pushed branch at exit.

## Tests
`pr_text` golden tests with fixture messages (all four section kinds, none,
hostile text); label table consistency between issue and PR paths; template
loader rejects missing sections; checker imports with `invoke` blocked
(proves stdlib-only); `put_file` list for the container is exactly the three
modules.
