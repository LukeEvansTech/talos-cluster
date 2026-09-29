# Todoist Sort

## Purpose

Two CronJobs in the `custom` namespace. `sort-inbox` sorts new Todoist Inbox items with an LLM
classifier, and `plan-day` promotes tasks due today up to a daily budget. Config, prompts and
routing rules are mounted from a ConfigMap built from `app/resources/`.

## Retry safety

- `sort-inbox` runs with a k8s-level `backoffLimit: 1`. A transient Todoist 5xx that outlasts the
  in-pod retry (seen 2026-07-12: about 6s of 503 on `GET /user`) fails the pod, and a respawn about
  10s later usually lands after the API recovers, turning what would page into a silent success.
  This is safe because the sort pass is idempotent (moved items leave the Inbox, flagged items carry
  the skip label), and the common failure mode is pre-LLM (`GET /user`), so a respawn re-spends no
  LLM budget.
- `plan-day` keeps `backoffLimit: 0` and must not be raised. Its promotion budget (`max_today` in
  `config.toml`) is tracked per process, so a k8s-level retry would start a second process that
  could promote up to `max_today` more tasks, breaking the daily cap. A transient LLM blip (for
  example, a LiteLLM cooldown seen on 2026-07-24) is instead absorbed in-process by `[llm]
retry_backoff` (todoist-sort >= 0.2.5), which shares the one budget. Whatever the in-pod retry
  can't recover self-heals at the next day's run.

## Renovate gotcha

`plan-day`'s container `image` block is a full duplicate of `sort-inbox`'s rather than an
anchor/alias. Renovate's helm-values extractor walks the resolved YAML, so an `image: *image` alias
becomes a second identical dependency whose digest-pin write corrupts the first
(`tag@sha256@sha256`), crashing the whole combined `renovate/pin-dependencies` branch (the
`renovate#24942` family of issues).
