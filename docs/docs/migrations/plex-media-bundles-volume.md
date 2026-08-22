# Plex Media Bundles Volume Migration

!!! danger "Not performed yet. Do not merge the manifests on their own."
    The manifest change and the data move are one operation. Landing the mount without first
    copying the data hides ~247G of live thumbnails from Plex, which then regenerates every one of
    them over a long GPU-bound rebuild while the originals sit unreachable on the config volume.
    Merge only when the window below is scheduled.

## Why

The `plex` PVC in `media` is the app's `/config` volume, and it is the volume VolSync backs up to
**both** targets: the Kopia repository on the NAS every four hours, and the restic repository in R2
every night. Measured inside the running pod:

| Path under `Plex Media Server/` | Size | Regenerable? | Backed up today |
| --- | --- | --- | --- |
| `Media` | 247G | Yes, from the library | Twice |
| `Metadata` | 36G | Partly (posters, agent matches) | Twice |
| `Plug-in Support` | 8.0G | **No.** The Plex database lives here | Twice |
| `Cache` | 1.5G | Yes | No, already its own PVC |

`Media` holds the per-item **media bundles**: the generated trickplay and BIF preview thumbnails
that render the timeline scrub preview, one bundle directory per library item under
`Media/localhost/0` through `Media/localhost/f`. Plex derives all of it from the media files
themselves. It is 85% of what gets stored, deduplicated, compressed, and shipped off-site, and none
of it is worth a single byte of either repository.

The same volume was 290G of 294G full on 2026-08-22 and was expanded to 500Gi in the same session.
It grows about 7.5G a week, essentially all of it here.

## What changes

`plex-cache` already establishes the pattern in this app: a standalone PVC mounted over a
regenerable subdirectory of `/config`, outside the `volsync` component's reach because VolSync only
ever snapshots `spec.sourcePVC`, which is `plex`. This adds a second one:

- `kubernetes/apps/media/plex/app/pvc.yaml` gains `plex-bundles`, 500Gi on `ceph-block`.
- `kubernetes/apps/media/plex/app/helmrelease.yaml` gains a `bundles` persistence entry mounting it
  at `/config/Library/Application Support/Plex Media Server/Media`.

`VOLSYNC_CAPACITY` in `ks.yaml` stays at **500Gi** and must not be lowered. It sizes the `plex` PVC
through `components/volsync/pvc.yaml`, and Kubernetes rejects a decrease with `field can not be less
than previous value`. The Flux apply fails, and the Kustomization wedges on every subsequent
reconcile. The volume is thin provisioned on Ceph, so an oversized claim costs nothing once the data
is gone; the 291G actually in use is what occupies the pool.

Raw pool usage does not change either. The bundles keep their 3x replication, they simply live in a
different RBD image.

## The alternatives considered, and why they were rejected

The obvious way to avoid a data migration is to leave the bytes where they are and teach the backup
to skip them. Three routes exist. All three were traced through the mover source at the version this
cluster runs, `ghcr.io/perfectra1n/volsync:v0.17.11`.

### The restic mover cannot express an exclusion at all

This is the decisive one. The remote (R2) `ReplicationSource` uses the **restic** mover, and its
backup command is a single hardcoded line in `mover-restic/entry.sh`:

```bash
"${RESTIC[@]}" backup --host "${RESTIC_HOST}" --exclude='lost+found' .
```

One literal exclude, no interpolation, no environment variable, no `--exclude-file`. The
`ReplicationSourceResticSpec` field list is `accessModes`, `cacheAccessModes`, `cacheCapacity`,
`cacheStorageClassName`, `capacity`, `copyMethod`, `customCA`, `moverAffinity`, `moverPodLabels`,
`moverResources`, `moverSecurityContext`, `moverServiceAccount`, `moverVolumes`, `pruneIntervalDays`,
`repository`, `retain`, `storageClassName`, `unlock`, `volumeSnapshotClassName` — byte identical to
upstream `backube/volsync`, and containing no exclusion field and no additional-arguments
pass-through. Whatever is done on the Kopia side, 247G keeps going to R2 every night.

### `spec.kopia.policyConfig` works, but only repository wide

`policyConfig.globalPolicyFilename` does carry ignore rules: the mover reads `files.ignore[]` out of
the JSON and turns each entry into an `--add-ignore=` flag. The problem is the command it appends
them to:

```bash
POLICY_CMD=("${KOPIA[@]}" policy set --global)
```

Every application in this cluster backs up into **one** Kopia repository at
`filesystem:///repository`, separated only by per-source username and hostname identity, and
`policy set --global` writes that repository's single shared policy object. Source identity buys no
isolation. Worse, every source mounts its data at the same `/data` root, so a rule is matched
identically against all of them; the merge is a union rather than a replace, so no per-path policy
can subtract it; and the mover only ever issues `--add-ignore`, never `--clear-ignore`, so a rule
added once persists for every source until someone removes it by hand against the repository. The
failure mode is silent — files simply stop appearing in everybody's snapshots, and it surfaces at
restore time.

`spec.kopia.additionalArgs` is not an escape hatch either. It reaches `kopia snapshot create`, but
`--add-ignore` and `--exclude` are flags of `kopia policy set`, not of `snapshot create`, so passing
them makes the backup fail on an unknown flag. (The CRD's own documented example,
`["--one-file-system", "--ignore-cache-dirs"]`, is invalid for the same reason.)

### A `.kopiaignore` file is genuinely per source, and still not enough

Kopia's built-in default policy honours `.kopiaignore` at the snapshot root, and VolSync never
touches dot-ignore settings or clears them. A `.kopiaignore` at the root of the `plex` PVC would
therefore exclude `Media` from the Kopia snapshots of that one source, with no shared state, no CRD
field, and no migration. It is the cleanest exclusion available, and it was seriously considered.

It still loses on two counts:

- It does nothing about R2. Half the problem, and the more expensive half, remains.
- It does not reclaim any space. The 247G stays on the `plex` claim, still counts toward
  `VOLSYNC_CAPACITY`, and the volume keeps filling at 7.5G a week — which is what triggered this
  work, since the volume hit 290G of 294G before being expanded.

Moving the data is the only option that fixes both targets and the disk at once. Its cost is a
maintenance window and a stateful cutover, which is what the rest of this page is.

## What this saves

- **Per-run backup dataset**: about 291G down to about 44G, an 85% reduction, on both targets.
- **Config volume**: about 291G down to about 44G used of 500Gi, and the 7.5G-a-week growth moves
  to a volume nobody backs up.
- **Repository size** is the honest caveat and it is not immediate. Kopia and restic both keep
  content alive as long as any retained snapshot references it. The NFS Kopia source retains
  `hourly: 168, daily: 90, weekly: 52, monthly: 24, yearly: 10`, so the existing 247G of bundle
  blobs stay in the NAS repository for **years** unless old snapshots are deleted by hand. The R2
  restic source retains `daily: 30` and prunes every 14 days, so that repository does reclaim on its
  own within roughly 30 to 45 days. Plan for "new backups get small immediately, the NAS repository
  only shrinks if you prune it".

## Before you start

Budget a **maintenance window of two to four hours** with Plex offline. The copy is roughly 247G in
several million small files, so it is metadata bound rather than throughput bound; two hours is a
reasonable expectation on `ceph-block` and four is a safe upper bound.

Confirm the starting state:

```console
$ kubectl -n media exec deploy/plex -c app -- df -h /config
Filesystem      Size  Used Avail Use% Mounted on
/dev/rbd4       492G  291G  201G  60% /config
```

```bash
# Slow (walks the whole tree); run it early, and record the number.
kubectl -n media exec deploy/plex -c app -- \
  du -sh "/config/Library/Application Support/Plex Media Server/Media"
```

Confirm both backups are current, so there is a restore point that still contains `Media`:

```bash
kubectl -n media get replicationsource plex-nfs plex-r2 \
  -o custom-columns=NAME:.metadata.name,LAST:.status.lastSyncTime,DURATION:.status.lastSyncDuration
```

Confirm the pool can take the second claim (thin provisioned, but check anyway):

```bash
kubectl -n rook-ceph get cephcluster -o jsonpath='{.items[0].status.ceph.capacity}'
```

## Runbook

Every step before step 6 is reversible by doing nothing. Nothing is deleted until step 9, which is
deliberately the last thing that happens and is gated on Plex having been observed healthy.

### 1. Suspend the HelmRelease, then merge

Suspending the **HelmRelease** but not the Kustomization is the whole trick: Flux still applies the
new `plex-bundles` PVC, while helm-controller leaves the Deployment alone, so the PVC exists before
anything mounts it.

```bash
flux -n media suspend helmrelease plex
```

Merge the pull request, then let the Kustomization reconcile:

```bash
flux -n media reconcile kustomization plex --with-source
kubectl -n media get pvc plex-bundles
```

Expect `Bound`, `500Gi`, `ceph-block`. Verify the running Deployment did **not** pick up the mount:

```bash
kubectl -n media get deploy plex -o jsonpath='{.spec.template.spec.volumes[*].name}{"\n"}'
```

The output must not contain `bundles`. If it does, the HelmRelease was not suspended in time; go
straight to Rollback A.

### 2. Suspend the Kustomization and remove the autoscaler

Plex carries the `zeroscaler` component, an HPA with `minReplicas: 0, maxReplicas: 1` driven by the
NFS probe. While that NFS probe reports healthy the HPA drives the Deployment back to one replica,
so `kubectl scale --replicas=0` alone will not hold. Delete the HPA for the window; resuming the
Kustomization at step 8 recreates it.

```bash
flux -n media suspend kustomization plex
kubectl -n media delete hpa plex
```

`just kube zeroscaler suspend` is not the tool for this. It pins zeroscaler HPAs **up** to
`minReplicas: 1` for node drains, which is the opposite of what is wanted here.

### 3. Stop Plex

`terminationGracePeriodSeconds` is 300 so that Plex checkpoints its SQLite database cleanly. Let it
have the full five minutes.

```bash
kubectl -n media scale deployment plex --replicas=0
kubectl -n media wait pod -l app.kubernetes.io/name=plex --for=delete --timeout=10m
```

`no matching resources found` from `wait` means the pod is already gone, which is the wanted state.

Optionally pause VolSync for the window so a mover run does not compete for the same RBD image:

```bash
just kube volsync suspend
```

Expect noise while Plex is down, and do not chase it: the Gatus check on `plex.${SECRET_DOMAIN}`
goes red, and `plex-exporter`, `tautulli`, `kometa`, `imagemaid` and `plex-auto-languages` all log
connection failures. Suspending `ks plex` also pauses reconciliation of the three Kustomizations
that `dependsOn` it (`kometa`, `imagemaid`, `plex-auto-languages`) until step 8 resumes it.

### 4. Copy the bundles

Both PVCs are `ReadWriteOnce`, and with Plex at zero replicas this Job is the only consumer of
either. It runs as root deliberately, so `rsync` can reproduce the source ownership exactly rather
than flattening it; `fsGroup: 1000` with `fsGroupChangePolicy: OnRootMismatch` makes the kubelet
stamp group 1000 on the empty target volume root, which is what stops Plex's own mount from trying
to recursively chown 247G on first use. `OnRootMismatch` matters on the source side too: the `plex`
volume root is already group 1000, so the kubelet skips it rather than walking 291G.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: plex-bundles-copy
  namespace: media
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 0
        runAsGroup: 0
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
      containers:
        - name: copy
          image: mirror.gcr.io/alpine:latest
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              SRC="/config/Library/Application Support/Plex Media Server/Media"
              apk add --no-cache rsync
              echo "=== copying"
              rsync -aHAX --numeric-ids --info=progress2,stats2 "$SRC/" /bundles/
              echo "=== target volume root ownership"
              chown 1000:1000 /bundles
              chmod 2775 /bundles
              echo "=== verification pass (expect no itemised lines)"
              rsync -aHAXn --numeric-ids --itemize-changes "$SRC/" /bundles/
              echo "=== sizes in 1K blocks (must match)"
              du -sk "$SRC" /bundles
              echo "=== entry counts (must match)"
              find "$SRC" | wc -l
              find /bundles | wc -l
          volumeMounts:
            - name: config
              mountPath: /config
            - name: bundles
              mountPath: /bundles
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: plex
        - name: bundles
          persistentVolumeClaim:
            claimName: plex-bundles
```

Save it as `plex-bundles-copy.yaml` outside the repository (this is a one-off operation, not
something Flux should ever reconcile), then:

```bash
kubectl apply -f plex-bundles-copy.yaml
kubectl -n media logs -f job/plex-bundles-copy
```

The Job is safe to delete and re-run: `rsync` resumes rather than restarting. If `rsync` refuses
`-A` or `-X` because the build lacks ACL or extended-attribute support, drop those two letters and
re-run; `-aH` preserves everything Plex actually depends on.

### 5. Verify the copy

Read the tail of the Job log and check all three signals:

- The verification `rsync --dry-run --itemize-changes` pass printed **no** itemised lines. This is
  the authoritative check: it compares every file's size, timestamp, ownership, and permissions.
- The two `du -sk` totals are identical.
- The two `find | wc -l` counts are identical.

Do not proceed on two out of three. If any disagree, re-run the Job; a mismatch after a clean re-run
means something is still writing to the source, which means Plex did not actually stop.

### 6. Rename the source directory

Delete the copy Job first so its completed pod releases the `ReadWriteOnce` attachment:

```bash
kubectl -n media delete job plex-bundles-copy
```

This next step is the first hard-to-undo one, and it is deliberately a rename rather than a delete.
After it, the original 247G still exists on the config volume as a sibling of the mount point,
reachable and restorable, just not where Plex looks.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: plex-bundles-rename
  namespace: media
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      containers:
        - name: rename
          image: mirror.gcr.io/alpine:latest
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              BASE="/config/Library/Application Support/Plex Media Server"
              mv "$BASE/Media" "$BASE/Media.pre-migration"
              ls -la "$BASE"
          volumeMounts:
            - name: config
              mountPath: /config
      volumes:
        - name: config
          persistentVolumeClaim:
            claimName: plex
```

```bash
kubectl apply -f plex-bundles-rename.yaml
kubectl -n media logs job/plex-bundles-rename
```

The rename is a same-filesystem operation, so it completes instantly regardless of size.

### 7. Cut over

Resuming the HelmRelease applies the new mount and rolls the Deployment back to one replica.

```bash
flux -n media resume helmrelease plex
kubectl -n media rollout status deployment/plex --timeout=15m
```

### 8. Resume the Kustomization and VolSync

```bash
kubectl -n media delete job plex-bundles-rename --ignore-not-found
flux -n media resume kustomization plex
kubectl -n media get hpa plex
just kube volsync resume
```

The HPA should be back at `0/1` minimum with one replica.

### 9. Verify Plex, then reclaim

Check the mount landed and both copies are visible:

```bash
kubectl -n media exec deploy/plex -c app -- df -h /config \
  "/config/Library/Application Support/Plex Media Server/Media"
```

At this point `/config` should still read about 291G used, because the old copy is still there, and
the new mount should read about 247G used of 492G.

Then check the thing that actually matters, in the Plex web interface:

- Open a recently watched title and scrub the timeline. Preview thumbnails must appear.
- Open a title from a different library, and one added long ago, to confirm it is not just one
  bundle directory that survived.
- **Settings → Manage → Troubleshooting** should show no new errors, and the pod log should be free
  of `Media` path errors.

Leave it at least **48 hours** before reclaiming. Until step 9b runs, Rollback B is still available.

#### 9b. Delete the old copy

```bash
kubectl -n media exec deploy/plex -c app -- \
  rm -rf "/config/Library/Application Support/Plex Media Server/Media.pre-migration"
```

This walks several million inodes and will take a while. If the `exec` session drops part-way the
delete stops where it was; just run it again, it is idempotent. Then confirm:

```console
$ kubectl -n media exec deploy/plex -c app -- df -h /config
Filesystem      Size  Used Avail Use% Mounted on
/dev/rbd4       492G   44G  448G   9% /config
```

The next scheduled `plex-nfs` run backs up about 44G instead of about 291G. `plex-r2` follows that
night.

### 10. Optional: reclaim the NAS repository

New snapshots are small immediately, but the existing ones still reference the old blobs and the
Kopia retention policy holds some of them for ten years. If the NAS repository size matters, delete
the pre-migration `plex` snapshots by hand through the Kopia server in `volsync-system`, then let
maintenance reclaim the blobs. Treat this as a separate, deliberate operation: it is the one step
that destroys the ability to restore the bundles from backup, and it should not be folded into the
migration window.

## Rollback

**Rollback A, before step 6.** Nothing has moved. Revert the pull request (or leave it merged and
keep the HelmRelease suspended), delete the `plex-bundles` PVC if it was created, resume the
HelmRelease and the Kustomization, and delete the copy Job. Plex comes back on the original layout.

**Rollback B, after step 6 and before step 9b.** The original data is intact at
`Media.pre-migration`. Suspend the HelmRelease, scale the Deployment to zero, run the rename Job in
reverse (`mv "$BASE/Media.pre-migration" "$BASE/Media"`), revert the pull request so the mount is
gone from the rendered HelmRelease, then resume. Delete the `plex-bundles` PVC once Plex is healthy.
Plex returns to the original layout with every thumbnail in place.

**After step 9b** there is no local rollback. Recovery is a VolSync restore of the `plex` PVC from a
pre-migration snapshot, which is exactly why step 9 insists on the 48-hour soak and why the
pre-flight checks confirm both backups are current.

## Follow-ups

- `VOLSYNC_CACHE_CAPACITY` for `plex` is 100Gi, sized when the source was 291G. It could come down
  once the source is 44G, but never below the 8Gi floor in
  [KB-030](../troubleshooting/kb/030-volsync-kopia-cache-pvc-too-small.md), and lowering it means
  deleting the existing cache PVCs, because those cannot shrink either. Not urgent; `miroir-local`
  is node local and cheap.
- `Metadata` at 36G is the next candidate by size, but it is a much weaker case: it holds agent
  matches, manual poster choices, and per-item edits that are **not** derivable from the media
  files. Regenerating it means re-matching the library. Leave it backed up.
