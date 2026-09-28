# rook-ceph

## Purpose

`rook-ceph` runs the Ceph storage cluster across three Kustomizations: the operator (`app`), the
CSI drivers (`csi-drivers`), and the cluster itself (`cluster`), which defines pools, the
filesystem, and Ceph's own configuration.

## mgr memory sizing

`cephClusterSpec.resources.mgr` must be set under `spec.resources.mgr` rather than a top-level
`spec.mgr.resources` field, because the `CephCluster` CRD has no such field and silently prunes it,
leaving the chart's 1Gi default in place.

With `diskprediction_local`, `insights` and `pg_autoscaler` enabled, plus the Prometheus and
dashboard endpoints, the active mgr's working set climbed past 2Gi and was OOMKilled about twice a
day, failing over to the standby each time. 3Gi holds it with headroom; the mon is already sized to
4Gi for the same reason.

## Mon clock skew threshold

`mon_clock_drift_allowed` is set to 0.3s, up from the 0.05s chart default. The nodes discipline
their clocks against public NTP over the WAN, and the kernel offset wanders in a sawtooth pattern
between polls: over a 30-day window, the peak observed `|offset|` was 111ms, with 27 excursions past
30ms spread evenly across all three nodes and unrelated to reboots. That routinely crossed the 50ms
default, so `CephMonClockSkew` fired and self-cleared on the next 300s `mon_timecheck_interval`, and
because the skew also puts the cluster in `HEALTH_WARN`, `CephHealthWarning` fired 15 minutes behind
it for the same event.

0.3s sits 2.7x above the observed peak and is 6% of `mon_lease` (5s), so a real time fault, which
shows up as seconds of drift rather than tens of milliseconds, still trips it. The setting is
runtime-settable, so Rook applies it without restarting the mons.

## CSI key rotation

`security.cephx.csi.keyType` is pinned to `aes` rather than left to the chart default. `AES256K`
needs a 7.0+ kernel, the fleet runs 6.18 even after upgrading to Talos v1.13.9, and both storage
paths here are kernel clients: `krbd`, and the built-in CephFS kernel client (see
`docs/docs/troubleshooting/kb/025-cephfs-modprobe-builtin-misdiagnosis.md`). Rook's rotation guide
recommends always setting the type explicitly, so a future default change upstream can't hand CSI a
key type the nodes can't use. No `keyRotationPolicy` is set for `csi`, so existing PV connections
are left untouched.

## Tearing down the CephFS filesystem

```console
ceph fs fail ceph-filesystem && ceph fs rm ceph-filesystem --yes-i-really-mean-it
```
