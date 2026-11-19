# Security

* [Threat model](threat-model.md) - The trust boundaries claude_sandbox draws around an agent session, and what each protection does and does not stop.
* [Protecting the default branch](branch-protection.md) - The rulesets that make PRs mandatory, the commit-metadata merge gate, required status checks, and the repo warnings shown to the host user.
* [Provenance](provenance.md) - Per-session SSH signing keys let the launcher prove every commit on a PR came from the session or from you.
* [Actions hardening](actions-hardening.md) - Why same-repo PR CI runs session code, which settings the launcher checks or offers, and the open question about workflow pushes.
* [Secret scanning](secrets-scanning.md) - Which credentials the launcher scans for, which it deliberately does not, and why GitHub push protection is a useful extra.
* [Optional rulesets](optional-rulesets.md) - Two admin-bypassable rulesets, applied by hand, that stop deploy keys creating arbitrary branches or pushing tags.
