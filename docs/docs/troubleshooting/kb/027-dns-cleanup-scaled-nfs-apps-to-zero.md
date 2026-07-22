# 027: A DNS cleanup scaled every NFS-backed app to zero

## Symptom

A dozen Gatus endpoints go red at once, within about sixty seconds of each other.
Affected apps span the `media` and `downloads` namespaces (Plex, Jellyfin, Sonarr,
Radarr, Bazarr, Pinchflat, SABnzbd, qBittorrent) plus their `-gluetun` sidecar
routes.

Every one returns **HTTP 503** on its normal hostname:

```text
[STATUS] (503) == 200   →   false
```

The hostname resolves correctly, so this does not look like a DNS fault.

## Why it is confusing

Three signals point away from the real cause:

1. **DNS resolves.** The Gatus check reaches something and gets a 503 back, so the
    record clearly exists.
2. **The gateway is healthy.** Other apps on the same listener are fine.
3. **The pods are not unhealthy. They are absent.** `kubectl get pods` simply does
    not list them, which reads like a scheduling problem rather than a DNS one.

A 503 from Envoy means *no healthy upstream*. With the Deployment scaled to zero
there is no upstream at all.

## Cause

The apps are governed by the `zeroscaler` component. Its HPA scales on an external
metric:

```yaml
metrics:
  - type: External
    external:
      metric:
        name: probe_success
        selector:
          matchLabels:
            job: nfs_probe
      target:
        type: Value
        value: "1"
```

`nfs_probe` is a blackbox TCP probe against `${SECRET_STORAGE_SERVER}:2049`. When
the NAS becomes unreachable the probe returns `0` and every NFS-backed app is
deliberately scaled to zero, rather than left with hung mounts. That behaviour is
working as designed: see [024](024-zeroscaler-nfs-hpa.md).

The failure is that **`SECRET_STORAGE_SERVER` is a hostname on the internal domain,
and its DNS record had been deleted** during a host-override cleanup.

## The trap: hostnames hidden inside secrets

The cleanup built its keep-list by grepping the repository for
`${SECRET_INTERNAL_DOMAIN}`. That is structurally incapable of finding the NAS,
because manifests never contain its hostname: they contain
`${SECRET_STORAGE_SERVER}`, and the hostname lives only in the `cluster-secrets`
Secret, sourced from 1Password.

Every other check agreed with itself and was wrong in the same way: the dry run,
the record-count assertion, and the sibling guard all reasoned about Git, so none
could see the reference.

A scan of `cluster-secrets` afterwards found **five** values carrying internal-domain
FQDNs: the storage server, the vSphere endpoint, and three network devices. Four
survived the cleanup only by luck: they had no primary-domain twin, so the
"redundant alias" filter skipped them anyway.

## Fix

Restore the deleted record, then confirm the metric recovers before checking apps.
The HPAs need a healthy probe before they will scale back up.

```bash
# 1. restore the record (uuid + values come from the cleanup's JSON backup)
#    POST /api/unbound/settings/addHostOverride  then  POST /api/unbound/service/reconfigure

# 2. the probe should return 1 within a minute
probe_success{job="nfs_probe"}

# 3. HPAs move from 0/1 REPLICAS 0 to 1/1 REPLICAS 1
kubectl get hpa -n media
```

Recovery took roughly four minutes end to end: about one minute for the probe, then
pods rescheduling.

## Prevention

Use `scripts/reclaim_stale_dns.py`. It derives its keep-list from **two** sources:
Git manifests *and* the values inside `cluster-secrets` / `cluster-settings`, and
refuses to delete anything appearing in either. It also prints a full JSON backup
with UUIDs before acting; capture that output, because it is the only route back
from a mistake.

The general lesson: when validating that something is unreferenced, enumerate every
place a reference can live. A value held in a Secret is invisible to `grep`, and a
check that only consults the repository will confidently report a clean result.

## Related

- [024: zeroscaler NFS scale-to-zero via native HPA](024-zeroscaler-nfs-hpa.md): why the apps scale to zero
- [Split DNS architecture](../../architecture/split-dns.md): the host-override ceiling and why
  `upsert-only` means cleanups are manual
