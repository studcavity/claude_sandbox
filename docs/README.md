# Documentation

Three OKF bundles, one per reader. Start at the repository
[README](../README.md) for the introduction.

| Bundle | For | Start |
| --- | --- | --- |
| [`host/`](host/index.md) | the operator who launches sandboxes | guides, deps-file reference, security model |
| [`session/`](session/github/index.md) | the Claude session inside a sandbox | copied into every container at launch, one variant per backend |
| [`dev/`](dev/index.md) | people changing the launcher | architecture, decisions, plans |

`session/` is source for what the launcher embeds, not a bundle to read in
place: its `common/` concepts and one backend directory are assembled at launch
(see [dev/architecture/session-docs.md](dev/architecture/session-docs.md)).
Operator pages never restate what a session is told; they link to it.
