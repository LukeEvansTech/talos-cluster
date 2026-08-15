# KB-030: VolSync Kopia Backups Fail with `no space left on device` on `/cache`

**Status:** Resolved for `wizarr` (cache raised 4Gi → 8Gi; first good sync 2026-08-15T19:18Z after
~15h of failures). The sizing rule below applies to every app using the `volsync` component.

## Symptom

One app's NFS backup mover pods pile up in `Error` while every other app backs up fine:

```console
$ kubectl get pods -n media | grep volsync-src
volsync-src-wizarr-nfs-7zzrt   0/1   Error      0   23m
volsync-src-wizarr-nfs-fxdpc   0/1   Error      0   32m
volsync-src-wizarr-nfs-xt8xz   0/1   Error      0   10m
```

The mover log fails at repository **connect**, not at backup:

```text
Attempting to connect to existing repository...
=== Connecting to existing repository ===
Connecting to filesystem repository
Using filesystem path: /repository
failed to open repository: unable to create format manager: unable to read format blob:
  error adding kopia.repository blob: unable to write cache directory marker:
  unable to write cachedir marker contents:
  write /cache/CACHEDIR.TAG: no space left on device
unable to remove cache directory: unlinkat //cache: read-only file system
Connection failed, creating new repository...
=== Creating repository ===
unable to get repository storage: found existing data in storage location
ERROR: Failed to create repository
```

Two lines make this look like something it isn't:

- **`found existing data in storage location`** is a red herring. It is the *fallback* path: the
  connect failed, so the mover tried to create a repository and correctly refused to clobber the
  existing one. The repository is fine — only the local cache volume is broken.
- **`Cannot determine current user: user: unknown userid 1000`** is printed by every Kopia mover
  (the movers run as UID 1000 with no `/etc/passwd` entry) and is unrelated.

The `ReplicationSource` keeps its last good `lastSyncTime`, so it looks stale rather than failing,
and a leftover `volsync-<app>-nfs-src` snapshot clone PVC hangs around from the wedged run.

## Cause

**The Kopia cache PVC is sized as if it held the app's data. It holds the shared repository's
index and metadata instead.**

Every app's `ReplicationSource` points at the *same* Kopia repository
(`KOPIA_REPOSITORY: filesystem:///repository` in `components/volsync/nfs/externalsecret.yaml`;
sources are separated by the username/hostname override, not by repository). On connect, Kopia
pulls that repository's index and metadata into `/cache` — so the cache footprint tracks the
**total repository size**, and is very nearly identical for every app regardless of how small the
app's own PVC is.

Measured on three apps backing up to the same repository:

| App        | Cache PVC | Used | `indexes` | `index-blobs` | `metadata` | `contents` |
| ---------- | --------- | ---- | --------- | ------------- | ---------- | ---------- |
| `wizarr`   | 4Gi       | 100% | 787M      | 1.2G          | 1.8G       | ~0         |
| `apprise`  | 8Gi       | 50%  | 787M      | 1.2G          | 1.9G       | ~0         |
| `atuin`    | 8Gi       | 34%  | 787M      | 1.2G          | 603M       | ~0         |

`indexes` and `index-blobs` are byte-identical across apps — that is the shared repository. Only
`metadata` varies. `contents` is empty, which matters: `cacheCapacity` becomes
`KOPIA_CACHE_CAPACITY_BYTES`, and that soft limit governs the **content** cache. Nothing evicts
index/metadata against it, so setting the cache PVC to a value only a little above the repository
index size does not leave headroom — it guarantees a 100%-full volume, and then even the 0-byte
`CACHEDIR.TAG` write returns `ENOSPC`.

`wizarr` was the only app in the cluster below 5Gi (`VOLSYNC_CACHE_CAPACITY: 4Gi` in its
`ks.yaml`), chosen to match its small 5Gi data volume. It was the only app failing.

## Fix

Raise `VOLSYNC_CACHE_CAPACITY` in the app's `ks.yaml` to the cluster floor and let Flux reconcile:

```yaml
postBuild:
  substitute:
    APP: *app
    VOLSYNC_CAPACITY: 5Gi
    VOLSYNC_CACHE_CAPACITY: 8Gi # not 4Gi — cache tracks repo size, not app size
```

Flux alone is not enough: the existing cache PVC is already full and VolSync will not grow it as
part of the mover run, so expand it too. `miroir-local` sets `allowVolumeExpansion: true`, so this
is online and non-destructive:

```bash
kubectl patch pvc volsync-src-<app>-nfs-cache -n <ns> \
  --type merge -p '{"spec":{"resources":{"requests":{"storage":"8Gi"}}}}'
```

**Order matters, and the two halves cannot be separated.** VolSync reconciles the cache PVC's size
from `ReplicationSource.spec.kopia.cacheCapacity`. The moment the PVC is 8Gi while the spec still
says 4Gi, the controller wedges on a shrink it is not allowed to make:

```text
PersistentVolumeClaim "volsync-src-<app>-nfs-cache" is invalid:
spec.resources.requests.storage: Forbidden: field can not be less than status.capacity
```

The mover pod itself still succeeds, but the controller never finishes its reconcile, so subsequent
runs stall. PVCs cannot be shrunk, so there is no way back — the only exit is `cacheCapacity: 8Gi`
in git. If the expansion is done before the change merges, patch the live `ReplicationSource` to
match and merge promptly, because Flux reverts it on the next reconcile (hourly) and reintroduces
the error:

```bash
kubectl patch replicationsource -n <ns> <app>-nfs \
  --type merge -p '{"spec":{"kopia":{"cacheCapacity":"8Gi"}}}'
```

Deleting the cache PVC instead of expanding it is safe (it is a cache — VolSync recreates it and
the run re-downloads the index), but only *after* the git change merges; before that it is
recreated at the old size and fails again. Either way, clear the wedged mover pods so the next
scheduled run starts clean:

```bash
kubectl delete pod -n <ns> -l app.kubernetes.io/created-by=volsync --field-selector status.phase=Failed
```

## Sizing rule

- **8Gi is the floor** for any app on the shared NFS Kopia repository. Do not scale it down for a
  small app — it is not proportional to `VOLSYNC_CAPACITY`.
- **The floor rises with the repository.** The index was ~2G of the 3.9G in use here; when peer
  caches routinely pass ~70%, raise the floor cluster-wide rather than app by app.
- Apps with genuinely large working sets (`plex` 100Gi, `jellyfin` 50Gi) size above the floor for
  their own content cache, which is a separate concern from this failure.

## References

- Related repo-size-drives-resources pattern:
  [KB-016](016-kopia-repo-server-oom-repo-size.md).
- Cache-volume exhaustion in a different subsystem:
  [KB-011](011-konflate-render-failures.md).
- Kopia caching: <https://kopia.io/docs/advanced/caching/>
