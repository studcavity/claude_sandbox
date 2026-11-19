# Guides

* [Getting started](getting-started.md) - Install requirements, launch a first sandbox, and choose the model and Claude login.
* [Local repositories (--local)](local-repos.md) - Work on a git repository that is not on GitHub: one branch goes in as a bundle, session branches come back as bundles.
* [Concurrent sessions and landing PRs](concurrent-sessions.md) - How several sandboxes share one repo, and how a session's branches become PRs that you merge.
* [Follow-up issues (todo/ files)](follow-up-issues.md) - How a session asks for a GitHub issue by committing todo/<name>.md, and why the issue is filed only after merge.
* [Cached images and disk space](images-and-caching.md) - How base and deps images are built, refreshed and kept, and how disk quotas keep one sandbox from filling the host pool.
* [Mounts, USB and nested containers](host-access.md) - Give a sandbox host directories, USB devices or nested Incus, each only with the host user's approval.
