# Operator documentation history

## 2026-11-19

* **Split**: `docs/` is now three bundles by audience. This one keeps the operator-facing guides, reference and security pages; architecture, decisions and plans moved to `dev/`; the deps-file schema moved to the session bundle and is linked from [the deps file page](reference/repo-deps-file.md).
* **Creation**: Reorganised the 637-line README and `claude_sandbox/README.md` into this bundle: guides, reference, security and architecture concepts. Moved `docs/specs/` to [decisions](../dev/decisions/index.md) and `docs/dev/plans/` into [plans](../dev/plans/index.md), adding frontmatter. Flag-by-flag detail was dropped in favour of `./run.sh --help`.
