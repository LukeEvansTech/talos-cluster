# Garage

Garage is the cluster's own S3-compatible object store, deployed as a 3-replica StatefulSet in
`kubernetes/apps/storage/garage` (namespace `storage`). `garage-webui` gives it a browser UI and
deploys as a dependent Kustomization alongside it.

## Least-privilege discovery RBAC

Garage's `kubernetes_discovery` feature manages `GarageNode` custom resources to find its own
peers. The CRD (`crd.yaml`) is vendored with `skip_crd = true` in `garage.toml`, so the app never
needs a cluster-scoped `customresourcedefinitions` grant: the Role and RoleBinding in `rbac.yaml`
cover only the namespaced `garagenodes` resource. The `garage` ServiceAccount itself has no
separate manifest; the HelmRelease creates it through `serviceAccount: { garage: {} }`.

## Secrets stay out of the ConfigMap

`rpc_secret` and `admin_token` never go into `garage.toml`. Both arrive as environment variables
(`GARAGE_RPC_SECRET`, `GARAGE_ADMIN_TOKEN`) from the `garage-secret` Secret, so the ConfigMap
holds no secret data. This keeps Trivy's KSV-0109 check ("secrets in ConfigMap") clean.

## automountServiceAccountToken must stay on

app-template turns `automountServiceAccountToken` off by default. Garage's `kubernetes_discovery`
needs the pod's own SA token to list and publish `GarageNode` CRs, so `helmrelease.yaml` turns it
back on explicitly.

## Metrics are unauthenticated by design

Garage exposes Prometheus metrics on its admin API port. `metrics_token` is unset in
`garage.toml`, so `/metrics` answers without authentication.

## Anonymous GET on the S3 API returns 403

The Gatus health check on the `s3` route treats both 200 and 403 as healthy, because Garage's S3
API answers an anonymous GET with 403.

## v2.4.0 startup panic (resolved)

Garage 2.4.0 panicked at startup with `kubernetes_discovery` enabled: its kube client built a
rustls `ClientConfig` with no crypto provider installed, which crashlooped the pod and stalled the
Helm rollback, so the image was held at v2.3.0 (#4980). `.renovaterc.json5` blocks only that one
release, so later patches flow through Renovate normally; 2.4.1 is what runs today (#5009).

## Cluster-to-NAS sync

`sync-cronjob.yaml` runs an hourly `rclone sync` from the in-cluster Garage to a Garage instance
on the NAS, because Garage has no native cross-cluster replication of its own. The NAS copy sits
on a ZFS-snapshotted dataset, which gives a point-in-time recovery option independent of this job.
The NAS `veeam` bucket is left out of the sync on purpose, because Veeam writes to it directly.

The job's two Garage access keys come from the `garage` 1Password item: one scoped to read the
in-cluster bucket, one scoped to write the NAS side. `RCLONE_VERBOSE` stays unset because pairing
it with the `--verbose` flag pushes rclone's logging to DEBUG, which prints the S3 secret access
keys in plaintext.

The sync target is addressed under the public `${SECRET_DOMAIN}` rather than
`${SECRET_INTERNAL_DOMAIN}`, even though the endpoint itself is internal only. The internal zone
is delegated to an internal resolver, so its wildcard certificate can no longer renew through
Cloudflare's DNS-01 challenge, and that zone is being retired.

## garage-webui secrets

`garage-webui` reuses the same admin token Garage itself uses (`GARAGE_ADMIN_TOKEN`) to call the
admin API, plus a separate login. `AUTH_USER_PASS` must be in `user:bcrypt-hash` form; both values
come from the `garage` 1Password item.

## References

- `kubernetes/apps/storage/garage/app/helmrelease.yaml`, `rbac.yaml`, `configmap.yaml` - the
  server, its RBAC and its non-secret config.
- `kubernetes/apps/storage/garage/app/sync-cronjob.yaml`, `sync-externalsecret.yaml` - the
  cluster-to-NAS backup job.
- `kubernetes/apps/storage/garage/webui/` - the browser UI and its ExternalSecret.
- `.renovaterc.json5` - the standing block on Garage `v2.4.0`.
