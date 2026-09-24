# Cluster operations: traps that read as something else

Read before running the local linter, querying the live cluster, or merging a batch of pull requests. Each item is a failure whose symptom points at the wrong cause.

## Linting this repository locally

- **Lint a throwaway sandbox, never the full checkout.** A super-linter run against this repository can hang in file enumeration for 12+ minutes, even with a correctly anchored `FILTER_REGEX_INCLUDE`, because the filter narrows what gets linted, not what gets enumerated. The tell is a log whose last line is `PARALLEL_COMMAND: parallel ...` with nothing after it. Copy the changed files plus `.editorconfig` and `.github/linters/` into a temporary directory, `git init`, commit, and lint that: about 90 seconds, with real per-linter results.
- **Kill a stuck container instead of waiting** (`docker kill`), and check for stale ones first: `docker ps --filter ancestor=ghcr.io/super-linter/super-linter:<version>`. A backgrounded wrapper around a killed run still reports exit code 0, which is not a pass.
- **Prove the run happened.** Capture `LOG_LEVEL=DEBUG` to a file; `grep -c 'Successfully linted'` should be 5 or more and every target path should appear. A filter regular expression is matched against `/tmp/lint/<path>`, so a repository-relative anchor matches nothing and exits 0 having linted nothing.

## Querying the live cluster

- **`kubectl` needs this repository's kubeconfig.** There is no `~/.kube/config`; `KUBECONFIG` is exported only by `.mise.toml` inside the checkout. Elsewhere, a bare `kubectl` fails with `the server could not find the requested resource`, which reads as a broken API server. Set `KUBECONFIG=<checkout>/kubeconfig` (and `TALOSCONFIG=talos/clusterconfig/talosconfig` for `talosctl`).
- **Ad-hoc Prometheus queries:** `kubectl -n observability port-forward svc/kube-prometheus-stack-prometheus 19090:9090` and hit `/api/v1/query`, or use the routed `prometheus.${SECRET_DOMAIN}` for a one-off.
- **`kubectl logs deploy/<x>` reads one pod.** On a multi-replica Deployment the log is a sample; a missing event may have landed on the other replica. Check `.status.replicas` first, and prefer the sender's own record over a receiver's log.
- **`kubectl run -i --rm` drops the first lines a short-lived pod prints**, because `-i` attaches after the container starts, so a loop's first iteration looks like it never ran. Run the pod without `-i`/`--rm`, `kubectl wait --for=jsonpath='{.status.phase}'=Succeeded`, read `kubectl logs`, then delete it.

## Merging batches of Renovate pull requests

- **`mergeable` goes `UNKNOWN` on every other open pull request after each merge**, because GitHub recomputes it asynchronously. A guard of `mergeable == MERGEABLE` read straight after a merge silently skips the rest of the batch. Poll `gh api repos/<owner>/<repo>/pulls/<n> --jq .mergeable` until it is not `null` (that REST read triggers the recompute), and retry once on `Base branch was modified`.
- **Drop `--delete-branch`.** It costs roughly 150 REST calls per merge and can empty the hourly budget within a batch; branches are deleted on merge by repository settings anyway.
- **REST and GraphQL are metered separately.** With REST at zero, `git push`, `gh pr merge --squash` and `gh api graphql` still work; check `gh api rate_limit` for `.resources.graphql`.
