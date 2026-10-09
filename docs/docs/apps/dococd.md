# doco-cd GitOps monitoring

## Purpose

doco-cd is the GitOps controller on each Docker host outside the cluster: the NAS, the seedbox and
the NUT appliance. Each host's own app (`truenas-exporter`, `seedbox`, `nut-appliance`) already
scrapes doco-cd's `/metrics` as a `<host>-doco-cd` job. `kubernetes/apps/observability/dococd` adds
one fleet-wide rule group and dashboard on top of those scrapes, so a new host is covered as soon
as its ScrapeConfig exists.

## Why this exists: 17 days of silent failure

From 2026-09-22 the NAS's doco-cd failed every hourly poll with
`failed to resolve external secrets: wasm error: out of bounds memory access`. The trigger was one
failed request to 1Password: the long-lived 1Password Go SDK client (v0.4.1) panics in its
`http_request` host function, and every later call fails until the process restarts
([1password/onepassword-sdk-go#288](https://github.com/1password/onepassword-sdk-go/issues/288)).
External secrets resolve all-or-nothing, so no stack on that host could reconcile, Renovate merges
included. Every container stayed up and healthy, so the container-lifecycle alerts had nothing to
see.

The only GitOps alert at the time watched `doco_cd_polls_total` on the NUT appliance and assumed it
counted successful polls. It counts every finished poll, failed or not (`handler_poll.go`
increments it after the job either way), so it could not have caught this. The failing host logged
24 polls and 24 poll errors a day throughout.

## Alerts (`dococd.rules`)

| Alert | Condition | Severity |
| --- | --- | --- |
| `DocoCDPollsFailing` | `increase(doco_cd_poll_errors_total[3h]) >= 3` for 15m | critical |
| `DocoCDStalled` | `increase(doco_cd_polls_total[3h]) == 0` for 30m | warning |
| `DocoCDDeployErrors` | any `doco_cd_deployment_errors_total` increase in 2h, for 10m | warning |
| `DocoCDDown` | `up == 0` for 15m | critical |
| `DocoCDVersionSkew` | more than one `doco_cd_info` version across hosts for 7d | warning |

`DocoCDPollsFailing` was backtested against the 14 days of retention when it was added: true on
the failing host for the whole window, never true on the other two. `DocoCDStalled` replaces the
NUT appliance's `NutApplianceGitOpsStalled` and now covers every host.

`DocoCDVersionSkew` exists because the controllers do not update themselves. Each repository pins
doco-cd in `bootstrap/compose.yaml` outside doco-cd's own `working_dir`, so a Renovate bump lands in
Git and nowhere else until the bootstrap is re-applied on the host. When the rule was added the
three hosts ran three different versions.

## Dashboard

The `doco-cd` dashboard shows, per host:

- last successful poll, computed as polls minus poll errors per hour, because `polls_total` alone
  counts failures;
- last successful deploy, the last rise of `doco_cd_deployments_total`, which doco-cd increments
  only after a deployment completes (`internal/docker/deployment.go`);
- poll errors in the last 24 hours, and the running version per host;
- polls, poll errors, deploys and deploy errors per hour.

The "last" panels look back over `max_over_time(...[14d:10m])`, so "none in 14d" means nothing in
Prometheus' retention, not necessarily never. A quiet repository deploys rarely, so an old
last-deploy time on its own is not a fault; the poll panels are the health signal.
