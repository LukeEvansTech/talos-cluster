# Curatorjudge

The taste half of the weekly Radarr library cleanup, in the `media` namespace. Reads the plan
`curator` left on the shared volume, asks Claude which candidates are worth keeping, and applies
whatever the engine still agrees with half an hour later. `curator`'s own
`app/resources/curator/README.md` covers the engine side; this page covers the judge's own
CronJob, its trust boundary, and how a failed run is meant to be read.

## Three steps, kept separate on purpose

The CronJob runs three containers in order: `prompt` builds the question from the plan, `judge`
asks Claude, `app` validates the answer, applies what still holds, and sends the digest. Only
`judge` talks to a model, and it is treated as the least-trusted step in the pipeline:

- It holds no credential beyond the Claude Code OAuth token (a separate `ExternalSecret` from
  the Radarr/Tautulli/Seerr/webhook one `app` uses).
- It gets no write access to the state volume at all (see below).
- Every answer it returns is re-validated by the executor against live Radarr immediately before
  each deletion, covering tags, collections, status, file presence and playback.

A wrong or hostile answer out of `judge` can therefore only choose badly among films the executor
independently re-clears; it cannot forge a candidate, bypass a protection, or reach the library
directly.

## The judge never sees the state volume

`persistence.state` in `app/helmrelease.yaml` is an `advancedMounts`, not a `globalMount`:
`prompt` gets it mounted read-only, `app` gets read-write, and `judge` gets no mount at all. With
write access, a compromised judge step (a malicious npm dependency, or a tool escape) could
rewrite `plan.json` to insert a protected film into `candidates`, which the executor trusts as the
base set. The executor's live re-checks cover tags, collections, status, file presence and
playback, not membership of the candidate list itself.

## Never retry: `backoffLimit: 0`

The CronJob sets `backoffLimit: 0` and `concurrencyPolicy: Forbid`. This pipeline ends in DELETEs,
and a Kubernetes-driven retry would re-run judgement and execution against a plan the previous
attempt may have already partly acted on. A non-zero exit here only marks the Job failed; it never
makes Kubernetes itself retry the pipeline. In the shared `curator` engine code, `cmd_execute`
treats a failed deletion (Radarr answered with a non-2xx status) as a safe no-op to retry, and an
uncertain one (a transport error after issuing the DELETE) as unsafe to retry: `_delete_one`
records it and leaves it rather than re-issuing the DELETE.

## The CLI installs at start-up

Anthropic publishes no public image for the Claude Code CLI, so the `judge` initContainer installs
it into a writable `/tmp` prefix at start-up (the root filesystem is read-only and the pod runs as
a non-root user). `CLAUDE_CODE_VERSION` is pinned in the manifest so the exact version is visible
in Git rather than resolved fresh on every run. The `judge` step calls it with `--max-turns 1` and
every tool denied: the model itself has no tool to touch a filesystem or make a network call, a
single structured judgement rather than an agent. This is a restriction on what the model can
invoke, not a network boundary on the container: nothing here stops the CLI process or a malicious
npm dependency from making its own outbound connection.

## Token lifetime

`CLAUDE_CODE_OAUTH_TOKEN` is generated with `claude setup-token`: a one-year, non-refreshable
token, the same shape as the Renovate reviewer token elsewhere in this repository (see the
[hardening backlog](../operations/hardening-backlog.md) for that token's own expiry gap). Give the
1Password item an `expires` date so 1Password Watchtower flags it before it lapses.
