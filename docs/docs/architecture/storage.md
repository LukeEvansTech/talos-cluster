# Storage

The cluster uses four storage tiers, chosen per workload:

- **Rook-Ceph**: replicated storage for stateful workloads that need durability and to move
  between nodes. Two StorageClasses: `ceph-block` (RBD, RWO: the default for app state) and
  `ceph-filesystem` (CephFS, RWX: for volumes shared across pods/nodes, e.g. the llmkube shared
  model cache).
- **miroir** (`miroir-local` StorageClass): loopfile-backed node-local volumes for workloads that
  want fast node-local storage and tolerate being pinned to a node. Unlike the retired OpenEBS
  hostpath, miroir enforces the requested PVC size (a real ext4 loopfile), so size volumes and
  VolSync caches deliberately.
- **NFS** (TrueNAS): bulk storage (media, etc.) and a backup target.
- **Garage** (S3): self-hosted S3-compatible object storage in the `storage` namespace, reached at
  `http://garage.storage.svc.cluster.local:3900` (region `us-east-1`, path-style). It replaced
  Rook's RGW/`CephObjectStore` and serves object-storage consumers such as CloudNativePG's
  barman-cloud backups and apps needing an S3 bucket.

## Choosing a tier

- Default to `ceph-block` for app state that must survive node loss and reschedule freely; use
  `ceph-filesystem` only when a volume genuinely needs RWX.
- Use `miroir-local` when the workload is latency-sensitive or explicitly node-local; remember it
  pins the pod and a single RWO claim cannot be mounted by pods on two nodes at once (Multi-Attach
  deadlocks show up on rollouts, so colocate the consumers).
- Use NFS for large shared datasets and as a VolSync destination.
- Use Garage when an app wants S3, provisioning the bucket and access key with the `/garage` CLI
  inside `garage-0` first.

## PostgreSQL databases

Apps share the CloudNativePG cluster `postgres18` in the `database` namespace. Each app's role and
database are declared in a tenant file, `kubernetes/apps/database/cloudnative-pg/tenants/<name>.yaml`,
applied by the Flux Kustomization `cloudnative-pg-tenants`. A tenant file holds an ExternalSecret
`<name>-pg` (the role password, read from the app's 1Password item), a `DatabaseRole` and a
`Database`, both with reclaim policy `retain`. Apps no longer create their own database with an init
container, and their Secrets carry no Postgres superuser password. The cluster has
`enableSuperuserAccess: false`, so no superuser password exists to leak; for admin work, run
`kubectl -n database exec -it <primary> -c postgres -- psql`, which uses the local socket.

`tenants/backup.yaml` is the exception: a role with no database. The hourly `postgres18-backup`
dump logs in as `pgbackup`, a member of `pg_read_all_data` with `BYPASSRLS` (prowler uses row-level
security), rather than the superuser. That role
cannot read large objects, so an app that starts storing them makes its database's dump fail.

### Adding a database for a new app

1. Copy an existing tenant file, such as `paperless.yaml`, and rename it. The role name equals the
   database name.
2. Add the file to `tenants/kustomization.yaml`.
3. Make the app's `ks.yaml` `dependsOn` `cloudnative-pg-tenants` (namespace `database`).
4. After merge, check `kubectl -n database get databaserole,database <name>`: both should show
   `APPLIED true`.

To rotate a password, update the 1Password item, then force-sync the tenant ExternalSecret
`<name>-pg` and the app's own ExternalSecret together. Each refreshes hourly on its own, so the role
and the app can disagree until both have synced. Removing a tenant file leaves the database and
role in place (`retain`); drop them by hand.

## Adding a subPath to an existing PVC

A non-root app (uid 1000, `fsGroup: 1000`) cannot write into a subPath directory the kubelet
creates: the kubelet makes it root-owned and not group-writable, and with
`fsGroupChangePolicy: OnRootMismatch` nothing repairs it, because the volume root already matches.
A volume restored from a snapshot taken before the directory existed hits the same wall. Create
the directory in an init container that mounts the volume **root**, as RomM does for Valkey's
`/redis-data` (#6341):

```yaml
initContainers:
  data-dir:
    image: *image # the app's own image, already pulled
    command: ["sh", "-c", "mkdir -p /cfg/data && chown 1000:1000 /cfg/data && chmod 2775 /cfg/data"]
    securityContext:
      runAsUser: 0 # chown needs root, even for a directory that already exists root-owned
      runAsNonRoot: false
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities: { drop: ["ALL"], add: ["CHOWN", "FOWNER"] }
```

Then mount the claim with `advancedMounts`, not `globalMounts`: the init container gets the root
(`- path: /cfg`) and only the app gets the subPath. A `globalMounts` subPath also reaches the init
container, and the kubelet creates the directory root-owned before the init container runs.
Define the `&image` anchor on the app container: YAML needs the anchor before the alias, so the
`initContainers` block goes after `containers`. The baseline pod-security default admits root with
`CHOWN` and `FOWNER`.

## Backups

PVC backups are handled by VolSync (Kopia to NFS, Restic to a remote R2 target); see
[Backups](../operations/backups.md) and the
[VolSync / Kopia migration](../migrations/volsync-kopia.md). A parallel `kopiur` operator trial
(CSI-snapshot-based Kopia backups on two apps) runs alongside VolSync; see Backups for details.
