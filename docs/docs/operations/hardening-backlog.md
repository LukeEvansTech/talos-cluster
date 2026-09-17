# Hardening backlog

A running list of **safety and observability defects found while doing other work**: things that
were quietly wrong rather than loudly broken. Append a finding the moment you hit one, even if it
is not the job you are on. A false green is worth more attention than a red, because it actively
buys confidence.

A finding earns a place here when it meets one of these bars:

- **A check that can pass without having run.** The worst class. A green that means "nothing
  looked" is indistinguishable from a green that means "everything is fine".
- **Documentation that asserts a safety property the cluster does not have.** Someone reading it
  makes a wrong decision with full confidence.
- **A guard whose empty or degraded state is fail-open.** The gate that lets everything through
  the moment it is unhealthy.

Conventions: number entries `H-NN` in order found, never reuse a number, and **mark resolved
entries rather than deleting them**. The resolved list is the pattern library for the next one.
Each entry records where it was found, the mechanism, and what closes it. Where a fix closed one
instance but not the class, say so; the class is what stays open.

## Open

### H-3: The Renovate review gate fails open on error, and its token expires

**Found:** 2026-06-03 while building `.github/workflows/renovate-review.yaml`; accepted by design.

`APPROVED` posts success and `CHANGES_REQUESTED` posts failure, but if the Claude step itself
errors (API blip, rate limit, timeout) the `claude/renovate-review` status **passes through** so
a transient blip does not wedge auto-merge. The OAuth token behind it has a one-year,
non-refreshable lifetime, generated 2026-06-02 and expiring **2027-06-02**. A lapsed token is an
error, so a lapsed token lets every in-policy Renovate PR auto-merge unreviewed.

**Mitigation in place:** a preflight step validates the token with a live call and turns a 401
into a red run. 1Password Watchtower on the token's item is the proactive alarm.

**What would close it:** a scheduled check that fails loudly (not the PR workflow) when the token
is within 30 days of expiry, or a policy that treats "errored" as "not approved" for the
high-blast-radius set only. See [Renovate auto-merge](renovate-automerge.md#ai-review-of-renovate-prs).

### H-5: A probe-killed graceful app is invisible to the alert named after the problem

**Found:** 2026-09-10, litellm restarted 62 times in two hours with no crashloop alert.

LiteLLM handles SIGTERM gracefully, so a liveness kill mid-boot exits `0` / `Completed`.
`CrashLoopBackOff` never triggers, so `KubePodCrashLooping` never fires, and a container that lives
about 100 s resets the backoff. Only a vague `KubePodNotReady` fired. Fixed for litellm in #5058
by raising the probe's initial delay to cover the measured 94 s cold boot.

**Still open, the class:** nothing alerts on a restart **rate** independent of the termination
reason. Any app that exits cleanly on SIGTERM can loop this way unseen. A rule on
`increase(kube_pod_container_status_restarts_total[1h]) > 3` regardless of reason would close it.

### H-6: Backup-metric absence is checked per box, not per tier

**Found:** 2026-08-03, verifying the docker-backup textfile hooks (#4087).

`DockerBackupMetricsMissing` asks `absent(...{box="seedbox"})`. The hourly NFS tier satisfies it,
so a permanently broken offsite (`r2`) tier on the same box alerts nothing, which is exactly the
silent death the rule exists to catch. `DockerBackupStale` cannot cover it either: a series that
never appears is not stale. Backrest also **silently discards unknown config fields**, so a config
that parses proves nothing about hooks being wired; verify by triggering a backup and reading the
side effect.

**What would close it:** per-box-and-tier `absent()` selectors, plus a check after each box's
first scheduled offsite run that the `tier="r2"` series exists.

### H-8: The CI render gate is lenient by construction

**Found:** 2026-06-13, testing whether Konflate could replace the flate workflow.

The `Konflate` commit status fails only on a total render `error`. Per-resource render `failures`
post success, and it does not schema-validate: a nonexistent chart tag and `replicas:
"two-not-an-int"` both passed. It was kept advisory and flate v0.3.x stayed the strict gate. The
flate GitHub Actions workflow was later removed (#3375), so Konflate is now the only render check
on a PR, with the local `just kube flate-test` as the strict check nobody is forced to run.

**What would close it:** either a strict render job back in CI, or confirmation that the current
Konflate version rejects an invalid manifest, tested with a deliberately broken PR (a known-bad
control). Until one of those is done, "Konflate is green" means "it rendered", not "it is valid".

### H-9: An operator-rendered rollout can be skipped with nothing reporting it

**Found:** 2026-09-03, after the Rook v1.20.6 to v1.20.7 bump (#4870).

Rook updated the CSI operator's image-set ConfigMap, but the `Driver` CRs reference that
ConfigMap by name and the ceph-csi-operator does not watch it, so all eight CSI plugin pods stayed
on the old cephcsi. The previous bump had been missed the same way; the CephFS nodeplugins were
55 days old. Nothing compares the declared image set to the running pods.

**Remedy per bump:** `kubectl -n rook-ceph rollout restart deploy/ceph-csi-controller-manager`
(see [upgrade playbooks](upgrade-playbooks.md#rook-ceph)).

**What would close the class:** a rule comparing `kube_pod_container_info` image tags in
`rook-ceph` against the image-set ConfigMap, or a post-merge health check that does the same. The
same shape exists anywhere a controller only re-renders on CR events.

### H-10: A completed Talos upgrade can revert and the CLI still exits 0

**Found:** 2026-09-03, five times, upgrading to 1.14.0.

If the reboot sequence's volume teardown fails (a loop device holding the encrypted `EPHEMERAL`
volume open), machined's fatal-error handler reverts the bootloader to the old UKI and the node
boots the **old** version. `talosctl upgrade --wait` returns 0 because it only waits for Ready.
tuppr does catch it (`version mismatch`), so the gap is only in the manual path.

**What would close it:** the manual upgrade recipe in `talos/mod.just` reading back
`osImage` after the reboot and failing on a mismatch. The loop-device detach that prevents the
revert is in [Talos upgrades](talos-upgrades.md#upgrade-didnt-take-node-reboots-into-the-old-version).

### H-11: The Renovate gate can re-post a verdict it did not compute

**Found:** 2026-09-14, on external-dns #5123.

The shared review workflow skips the model and re-posts the prior verdict whenever the package
versions in the diff are unchanged since the last review. When the fix for a `CHANGES_REQUESTED`
belongs on `main` (a chart value, a bootstrap helmfile) rather than in the PR, landing it and
rebasing changes nothing the gate looks at, so the PR stays red on a verdict from before the fix.
The status reads as a fresh review and is not.

**Workaround:** verify the fix at the point of consumption, then admin-merge. **What would close
it:** the fingerprint including the base SHA of the files the review cited, so a fix on `main`
invalidates it. Lives in `shared-workflows`.

### H-12: `ShellyDeviceOutOfSync` cannot distinguish drift from unreachable

**Found:** 2026-07-14, the Shelly firmware 2.0.0 nonce-throttle incident.

The operator's `inSyncValue()` maps an `Unknown` condition (config fetch failed, HTTP 429) to `0`,
the same as `False` (drifted). The alert then claims config drift with `driftedSections` empty.
The churn that caused the 429s was fixed in shelly-operator v0.3.2, but the conflation remains, so
the next unreachable device will page as drift.

**What would close it:** a state or reason metric from the operator and an alert expression that
excludes `Unknown`. Lives in the shelly-operator repository.

### H-13: The Scrypted page documented an app that was never commissioned

**Found:** 2026-08-25.

`docs/docs/apps/scrypted.md` described a working NVR design. The live app had only the core plugin
installed and no admin user since deploy. The page recorded intent as if it were state.

**What would close it:** a **Status** line at the top of every app page stating what is verified
live and when, the way the KB entries already do. Not yet applied to any app page, scrypted
included.

## Resolved

Mark, do not delete. Each one is a pattern that will recur in a different place.

### H-19: Cached Fireshare videos bypassed a newly set password (resolved 2026-09-17)

**Found:** during the public deployment test. Fireshare's `/_content/video/` responses carry
`Cache-Control: public, max-age=31536000, immutable`. After anonymous playback followed by
setting a password, the API returned HTTP 403 but Cloudflare still served the cached video
with HTTP 206. Deleting a video likewise cannot revoke an already cached response.

**Fix:** the Fireshare HTTPRoute overwrites `Cache-Control` with `private, no-store`, so the
access gate runs on subsequent requests. Verify using a new test video: anonymous range
playback succeeds, then setting a password makes both media endpoints return HTTP 403.
Previously cached content requires a separate purge; only generated test clips were uploaded
before applying this fix, and no existing media library was mounted.

**Deletion follow-up:** even with HTTP caching disabled, nginx's `open_file_cache` kept
deleted files readable until its 120-second revalidation. The startup command disables that
cache as well. A repeated static media request after deletion must return HTTP 404; the
upstream API reports a missing video as HTTP 500, but must not return video bytes.

### H-18: Fireshare hid anonymous uploads without disabling them (resolved 2026-09-17)

**Found:** while deploying Fireshare 1.8.1. Its default `show_public_upload: false` hides the
upload button, but `allow_public_upload: true` still permits anonymous API uploads.

**Fix:** the Fireshare init container sets `allow_public_upload: false` before every startup,
along with private defaults for new media. An invalid configuration stops initialization rather
than falling back to upstream defaults. Verify both `/api/upload/public` and
`/api/uploadChunked/public` reject unauthenticated POST requests with HTTP 401.

### H-1: The Gatus down-alert rule was never loaded (resolved 2026-07-01, #3372)

`observability/gatus/app/kustomization.yaml` had `# - ./prometheusrule.yaml` commented out since
2025-03-19. Two PRs (#3220, #3221) edited the rule's expression and recorded "117 apps now
alertable"; the rule was in a file Flux never applied. Live Prometheus had 343 alert rules and 0
for Gatus, and an internal app that was down for 20 minutes never paged while the pipeline was
demonstrably working for nine other alerts.

**Lesson:** verify a PrometheusRule is **loaded** (`/api/v1/rules`, or `ALERTS_FOR_STATE` for the
alert name), not that the CR or the file exists. The fix also switched the expression from an
allowlist of groups to a denylist (`group!="connectivity"`) so new groups are covered by default.

### H-2: tuppr metrics were never scraped (resolved 2026-06-02, #2747)

The hand-written alerts in `tuppr/upgrades/prometheusrule.yaml` had no data behind them: no
ServiceMonitor existed anywhere. Found only by comparing against upstream's chart values.
**Lesson:** an alert rule with no matching series never fires and never errors. For every new
rule, query the metric it depends on and confirm a non-empty result.

### H-4: `UpsLowRuntime` could never match (resolved 2026-07-03, #3397)

`nut_ups_status{status="OB"} and nut_battery_runtime_seconds < 600` never matched: the left side
carries a `status` label the right side lacks, so the vector match was always empty. Found after
the mains-loss incident it should have paged for. The same PR found the two-endpoint
ServiceMonitor quadruplicating every UPS series, so every expression needed `max by (ups)`.
**Lesson:** test a rule with `promtool test rules` against a fixture that should fire, and
against one that should not.

### H-7: `MikrotikPoeDeviceLostPower` resolved itself while the device was still dead (resolved 2026-08-28, #4721)

The evidence clause was `min_over_time(...[6h] offset 30m) > 0`. Thirty minutes into an outage the
first zero entered the window, `min` dropped to 0, and the alert announced "resolved" about a dead
device. Caught by the PR reviewer, not by a test. **Lesson:** an evidence term ("it used to be
good") is `max_over_time`; `min_over_time` is for "held continuously", which is rare. The comment
in the rule had documented a dwell time the expression never had, so the `promtool` test now
asserts the dwell.

### H-14: Intermittent partial loss never tripped a `for:` (resolved 2026-08-27, #4684, #4685)

The core switch's SNMP target was down 29% of the time in stalls mostly under five minutes, for
two days, and `SnmpTargetDown` (`for: 5m`) sat one longer stall from paging while firing nothing.
**Lesson:** for flapping targets alert on a rate over a window (`1 - avg_over_time(up[1h]) > 0.5`),
not on a sustained state. Written as `1 - avg` so `$value` is the failure fraction the summary
quotes.

### H-15: `ShellyDevicePendingFirmware` could never fire (resolved 2026-07-15, shelly-operator v0.3.2)

`status.availableFirmware` was empty on all 49 devices, so `shelly_device_update_available` was
pinned at 0 and firmware 2.0.0 landed on three devices unannounced. Found while chasing the
incident it would have warned about. **Lesson:** a metric pinned at its "all clear" value across
the whole fleet is suspicious, not reassuring; check it can take the other value.

### H-16: The `cluster-secrets` placeholder was re-applied over the real values hourly (resolved 2026-08-03, #4081)

kustomize-controller re-applied the placeholder Secret (`SECRET_DOMAIN: example.com`) on every
reconcile, opening a one-second window in which a HelmRelease could render a route with the
placeholder hostname, and Helm's three-way merge never repaired the drift afterwards. Fixed with
`kustomize.toolkit.fluxcd.io/ssa: IfNotPresent`. **Lesson:** verify a fix through the mechanism
that proves it (the managedFields timestamp stopped advancing), not through the artifact the
mechanism rewrites (the annotation is absent on the live object, by design).
[KB-020](../troubleshooting/kb/020-httproute-drifts-to-placeholder-hostnames.md).

### H-17: `just lint` was stricter than CI (resolved, #3759)

The local super-linter wrapper lacked the `docs/` exclusion CI applies, so it failed on files CI
never checks and trained people to ignore its output. **Lesson:** a local gate that disagrees with
CI in either direction stops being run; keep them byte-identical in configuration.
