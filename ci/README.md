# ci/

`tests.yml` is the GitHub Actions workflow for this repo. It belongs in
`.github/workflows/tests.yml`, but sandbox deploy keys can't push workflow
files, so it is parked here. Install it from a host with the `workflow` scope:

```sh
git pull && git mv ci/tests.yml .github/workflows/tests.yml && git rm -q ci/README.md
git commit -m "ci: enable tests workflow" && git push
```
