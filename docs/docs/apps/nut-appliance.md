# NUT appliance monitoring

## Purpose

The NUT appliance is a small FCOS box that exports UPS status alongside a handful of
doco-cd-managed containers. `kubernetes/apps/observability/nut-appliance` scrapes its host, SMART,
Docker and doco-cd exporters over a plain LAN address and provisions its dashboard and alerts.

## Why a bespoke dashboard

The upstream "Node Exporter Full" (grafana.com 1860) doesn't fit this box for two reasons: it has
no RAPL panel, and RAPL power is the whole reason node-exporter runs as root here; and its rootfs
panel keys on `mountpoint="/"`, which node-exporter does not expose on this host at all (confirmed
against live Prometheus: zero series). The panel would render empty rather than useful, so the
bespoke `nut-appliance` dashboard uses `/var` instead, the filesystem FCOS actually exposes (see
"The mountpoint quirk" below). Every query was validated against live data before merge, same
shape as the seedbox and truenas-zfs dashboards.

## The `$__rate_interval` bug (#3919)

The rate() panels use an explicit `[5m]` window rather than Grafana's `$__rate_interval` macro.
That macro expands to `max($__interval + scrape, 4 * scrape)`, where `scrape` is the **datasource's**
configured interval (15s by default) regardless of what the job actually scrapes at. This job
scrapes at 60s, so Grafana was observed sending `rate(...[1m15s])` against it, a window that rarely
catches two samples. All three rate panels rendered "No data" while every other panel was fine.

Setting the panel's Min step alone does not fix it: Min step raises `$__interval`, but the `scrape`
term in the macro still comes from the datasource setting, not the job. An explicit window sidesteps
the macro entirely and is deterministic regardless of datasource settings. Any other dashboard in the
estate that pairs `$__rate_interval` with a scrape interval slower than 15s has the same latent
problem; seedbox's own dashboard carries the same fix and points back here.

## Alert design: four host-level rules, wide margins

`nut-appliance.rules` carries seven alerts in total: four host-level ones covered in this section
and three container-lifecycle alerts (covered further down). Its former GitOps-stalled alert moved
to the fleet-wide [doco-cd rules](dococd.md), which cover every doco-cd host. Every
alert in this host-level group pages. Alertmanager's root route sends all severities to Pushover,
so `warning` is not a quiet tier here, and the seedbox learned the cost of forgetting that (an
iowait alert calibrated at the textbook threshold "would have paged perpetually"). So the group is
deliberately small, with margins wide enough that only a real problem crosses them, rather than a
comprehensive sweep of everything a generic exporter can measure.

The four host-level rules are calibrated against values observed on the box on 2026-07-28, not
textbook defaults:

| Metric    | Observed baseline               |
| --------- | ------------------------------- |
| Load      | 0.08 across 6 cores             |
| Memory    | 836 MB of 7796 MB used, no swap |
| `/var`    | 8.2 GB of 238 GB (3.4%)         |
| CPU temp  | 35-36°C                         |
| NVMe temp | 33-35°C                         |

### Not shipped: a RAPL power-regression alert

The power series is the reason node-exporter runs as root on this box, but no alert keys on it. A
prior tuning pass moved package idle draw from 525 mW to 411 mW, and the same tuning notes recorded
394 mW vs 411 mW as indistinguishable run-to-run noise, so a threshold separating "tuned" from
"untuned" sits inside the noise floor. A fresh 300s sample on 2026-07-28 read 261 mW, 36% below the
figure recorded five days earlier, with package C-state residency shifted from PC8 65% to PC9 85%.
The band isn't stable enough to threshold yet. It's graphed on the dashboard instead; revisit once
there's a couple of weeks of history and a real working band. Shipping a number now would be
guessing, the same mistake the seedbox iowait alert exists to warn against.

## NutApplianceHostMetricsMissing

Deliberately not a "box is down" alert: `UPSUnreachable` already covers that, at Pushover EMERGENCY
priority with a 3m fuse tuned to the main UPS's roughly 13-minute battery, and a second critical here
would only double-page. What that alert can't see is host metrics dying while `upsd` keeps serving:
a crashed exporter, a Docker problem, a bad deploy. Gating on the nut-exporter scrape still
succeeding turns this rule into exactly that statement, the appliance is alive but blind on it.

The gate also keeps FCOS auto-updates quiet: Zincati reboots the box periodically, which takes upsd
down too, so the gate goes false and nothing fires. The 15m `for` additionally rides out the reboot
itself. One comment in an earlier draft of this rule claimed a consistent 50-second NIC link-up delay
on every boot; that specific figure could not be re-verified from this repository and is not carried
forward here as a stated fact.

`min()` over `job=~"nut-appliance-.*"` covers every appliance scrape (node, smartctl, Docker,
doco-cd, traefik) in one rule rather than five near-identical ones. The operator response is the same
whichever exporter dropped, and every extra rule is another thing that can page on its own.
`max()` collapses nut-exporter's two ServiceMonitor endpoints (one per UPS) to a single scalar so
`and on()` has an empty label set to match against.

## Container lifecycle: three rules, not five

`truenas-docker.rules` carries five container-lifecycle alerts; this box only needs three. OOM is
already covered by `NutApplianceMemoryLow` plus the down/flapping rules showing the aftermath, and
`container_state_oomkilled` is sticky until the container is recreated, so it would re-page on every
`repeatInterval`, a poor trade on a box this small. Rules are scoped to the doco-cd-managed fleet so
a hand-run throwaway container, exactly what the exporters were tested as, can never page.

These overlap `UPSUnreachable` when `nut-upsd` itself dies, deliberately: "container nut-upsd exited"
is directly actionable where "UPS monitoring is blind" is only a symptom, and the two alerts use
different names so the critical-to-warning inhibit rule doesn't couple them.

The restore-init one-shot exclusion (`name!~".*-restore-.*-[0-9]+"`) and the `created` status trace
back to the same fleet-wide incident as seedbox's (#4088, see
[seedbox monitoring](seedbox.md#one-shot-containers)): restore inits exit 0 and stay exited, and a
failed one lands in `created` instead of `exited`. Seedbox later moved to an `alerts.oneshot=true`
label; this app still excludes by name, so a new one-shot container here needs a name matching
`.*-restore-.*-[0-9]+` or it will page.

## The mountpoint quirk

`mountpoint="/var"` is load-bearing in `NutApplianceDiskFillHigh`. FCOS mounts the same xfs
filesystem at four places (`/var`, `/etc`, `/sysroot`, `/sysroot/ostree/deploy/...`), all reporting
identical numbers, so an unpinned expression would fire four identical alerts for one problem.
`"/"` must not be used either: FCOS mounts it as a small composefs overlay that's permanently full
at the OS level, but node-exporter doesn't expose that mountpoint as a metric on this host at all
(confirmed live). A rule keyed on it wouldn't misfire, it would simply never evaluate, a silent gap
rather than a working safety net.

`/boot` is deliberately excluded from disk-fill alerting. It sits at 350 MB, 48% used, and 85% of
that is only 52 MB free, already past the point where `rpm-ostree` can stage a kernel. A threshold
on `/boot` would be useless at that size; if it ever needs watching, it needs its own rule and its
own number rather than a share of this one.

## Checking the unscoped ceph-mixin rules

Several `ceph-mixin` alerts ship with no job selector and therefore adopt every node-exporter target
in the cluster, including this appliance. `CephNodeDiskspaceWarning` joins through `node_uname_info`
(`* on(cluster, instance) group_left(nodename) node_uname_info`), so the ScrapeConfig drops that
metric here to keep a 5-day `predict_linear` on the appliance's filesystems from paging; nothing else
reads it, and the dashboard deliberately carries no kernel-version panel. The seedbox drops the same
metric for the same reason.

The other two unscoped ceph node alerts were checked against this box rather than assumed safe:

| Rule                         | Check                                                                                                                                                                                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `CephNodeRootFilesystemFull` | Keys on `mountpoint="/"`, which node-exporter doesn't expose on this host at all (see "The mountpoint quirk" above), so the rule can never fire here.                                                                                                  |
| `CephNodeInconsistentMTU`    | Compares each device against the cluster-wide median for that device name. Verified: `eno1` 1500, `docker0` 1500 and `tailscale0` 1280 all match the existing medians, and the `br-*`/`veth*` names are unique to this host so each is its own median. |
