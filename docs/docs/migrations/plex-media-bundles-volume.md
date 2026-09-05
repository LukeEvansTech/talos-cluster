# Plex media bundles volume migration

!!! danger "Not performed yet. Do not merge the manifests on their own."
    The manifest change and the data move are one operation. Landing the mount without first
    copying the data hides ~247G of live thumbnails from Plex, which then regenerates every one of
    them over a long GPU-bound rebuild while the originals sit unreachable on the config volume.
    Merge only when the window below is scheduled, and note that the procedure does not end at the
    cutover: steps 10 and 11 reclaim the config volume and purge the superseded snapshots from the
    shared NAS repository, and both are required.

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

500Gi on `plex-bundles` is about eight months of headroom at the current growth rate. Unlike the
config volume, growing it later is a one-line edit with no wedging risk: it is a plain claim, not
one sized through `VOLSYNC_CAPACITY`, and `ceph-block` has `allowVolumeExpansion: true`.

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
`repository`, `retain`, `storageClassName`, `unlock`, `volumeSnapshotClassName`, byte identical to
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
failure mode is silent. Files simply stop appearing in everybody's snapshots, and it surfaces at
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
  `VOLSYNC_CAPACITY`, and the volume keeps filling at 7.5G a week, which is what triggered this
  work, since the volume hit 290G of 294G before being expanded.

Moving the data is the only option that fixes both targets and the disk at once. Its cost is a
maintenance window and a stateful cutover, which is what the rest of this page is.

## What this saves

- **Per-run backup dataset**: about 291G down to about 44G, an 85% reduction, on both targets.
- **Config volume**: about 291G down to about 44G used of 500Gi, and the 7.5G-a-week growth moves
  to a volume nobody backs up.
- **Repository size, R2 side: no action needed.** The restic source retains `daily: 30` with
  `pruneIntervalDays: 14`, so the pre-cutover snapshots age out and the space comes back on its own
  within roughly 30 to 45 days. Do not prune it by hand.
- **Repository size, NAS side: action needed, and it is step 11.** The Kopia source retains
  `hourly: 168, daily: 90, weekly: 52, monthly: 24, yearly: 10`, so left alone the 247G of bundle
  blobs would sit in the shared repository for up to ten years. Step 11 deletes the pre-migration
  snapshots for this one source; the space returns 24 to 48 hours later, once maintenance has
  cleared its safety gates.

## Before you start

Budget a **maintenance window of two to four hours** with Plex offline, but expect to need far less
of it. The estimate assumed the copy would be metadata bound rather than throughput bound; on this
cluster's NVMe-backed `ceph-block` it was not.

Measured on the 2026-08-23 run: **Plex was offline for 14 minutes 30 seconds**. The copy itself moved
248G in 410,266 entries in about nine minutes, averaging 525 MB/s. Keep the wide window booked. It
costs nothing and the failure modes below all want unhurried attention, but do not plan the day
around four hours.

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

Every step before step 6 is reversible by doing nothing. Nothing is deleted until step 10, which is
deliberately late and gated on Plex having been observed healthy for 48 hours. Steps 10 and 11 are
required, not tidy-up: without step 11 the whole point of the change is only half delivered on the
NAS side.

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
              echo "=== entry counts excluding lost+found (must match exactly)"
              find "$SRC" | wc -l
              find /bundles -path /bundles/lost+found -prune -o -print | wc -l
              echo "=== file and directory counts (must match exactly)"
              find "$SRC" -type f | wc -l
              find /bundles -type f | wc -l
              find "$SRC" -type d | wc -l
              find /bundles -type d -path /bundles/lost+found -prune -o -type d -print | wc -l
              echo "=== allocated blocks (ADVISORY - will differ, see step 5)"
              du -sk "$SRC" /bundles
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

Read the tail of the Job log. **One signal is authoritative and two are advisory**, which is a
correction to the original wording. As written, two of the three fail on a perfectly good copy.

The authoritative check:

- The verification `rsync --dry-run --itemize-changes` pass printed **no** itemised lines. It
  compares every file's size, timestamp, ownership and permissions. If this is clean, the copy is
  clean. If it lists anything, re-run the Job; entries surviving a clean re-run mean something is
  still writing to the source, which means Plex did not actually stop.

The two advisory checks, and why neither can be an equality test:

- **`find | wc -l` differs by exactly one.** A freshly formatted volume carries a `lost+found`
  directory that the source does not. Compare with it excluded:

  ```sh
  find "$SRC" | wc -l
  find /bundles -path /bundles/lost+found -prune -o -print | wc -l
  ```

- **`du -sk` totals will not match, and should not be expected to.** `du -sk` reports *allocated
  blocks*, not content. The source has been written and rewritten for over a year, so several
  thousand of its 161,479 directories hold an extra 4K block that the freshly written target packs
  away. On the 2026-08-23 run the target was **13,884 KB smaller** with byte-identical content.
  Compare file and directory counts instead, which are exact:

  ```sh
  find "$SRC" -type f | wc -l   # must equal the target's
  find "$SRC" -type d | wc -l   # must equal the target's
  ```

  Note busybox `du` in the Alpine image has no `-sb`, so apparent size is not available there
  without installing coreutils.

For reference, the 2026-08-23 run: itemise pass clean, entries 410,266 on both sides once
`lost+found` was excluded, 161,479 directories and 248,787 files on both sides, and a `du -sk` delta
of 13,884 KB that was purely block allocation.

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

### 9. Verify Plex, then soak

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

Leave it at least **48 hours** before reclaiming. Until step 10 runs, Rollback B is still available.

### 10. Reclaim the config volume

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

### 11. Purge the pre-migration snapshots from the NAS repository

This step is **required**, not optional, and it is the only place in this runbook where the shared
backup repository is written to. Read the whole step before running anything in it.

New snapshots are small from the first run after step 10, but the old ones still reference the 247G
of bundle blobs, and `plex-nfs` retains `hourly: 168, daily: 90, weekly: 52, monthly: 24,
yearly: 10`. Left alone, those blobs stay in the repository on the NAS for up to ten years. Deleting
the pre-migration `plex-nfs` snapshots is what actually returns the space.

**The R2 side needs nothing, and must not be pruned by hand.** All 98 restic sources carry
`pruneIntervalDays: 14` and `retain: {daily: 30}`, and every one of them has a recent
`status.restic.lastPruned` (checked 2026-08-22: none missing, oldest 15 days old). The pre-cutover
R2 snapshots therefore age out and the space is reclaimed on their own within roughly 30 to 45 days.
Confirm rather than intervene:

```bash
kubectl -n media get replicationsource plex-r2 \
  -o custom-columns=NAME:.metadata.name,PRUNED:.status.restic.lastPruned
```

#### This is a shared repository

Around 200 replication sources write into the one `filesystem:///repository` on the NAS. They are
separated only by per-source Kopia identity: this app's is `plex-nfs@media`, snapshotting `/data`.
There is no per-app repository, no per-app credential, and no isolation beyond that identity string.

Every command below names `plex-nfs@media:/data` explicitly. **Do not run anything repository-wide.**
Specifically, none of these belong anywhere near this step, and all of them exist in Kopia 0.23.1:

| Command | What it would do |
| --- | --- |
| `kopia snapshot delete --all-snapshots-for-source plex-nfs@media:/data` | Deletes **every** `plex` snapshot, the good post-migration ones included. The flag also silently changes what the positional argument means: with it set, the argument is read as a source spec rather than a snapshot ID. |
| `kopia policy set --global …` | Rewrites the single policy object shared by all ~200 sources. |
| `kopia maintenance set …`, `kopia maintenance run …` | Maintenance is owned by the `maintenance@volsync` identity and driven by the hourly CronJob. Running it by hand can take ownership away and stop that job silently. |
| `kopia blob delete`, `kopia blob gc`, `kopia content delete` | Repository-wide storage destruction. All three are hidden commands gated behind `--dangerous-commands=enabled` / `KOPIA_DANGEROUS_COMMANDS`; upstream's own refusal message reads "Running this command is not needed for using Kopia. Instead, rely on periodic repository maintenance." Do not set that variable. |
| `kopia repository set-parameters …` | Changes the repository for every source at once. |

One more trap, learned the hard way: **do not run the Kopia CLI inside the `kopia` pod in
`volsync-system`.** That container is capped at 2Gi and the server process already sits near it, so a
second Kopia process loading the repository indexes OOM-kills the server (`exit 137`). It restarts by
itself, but the backup web interface is down while it does. The Job below runs in its own pod with
its own memory budget for exactly this reason.

#### 11a. Record the "before" figure

The hourly maintenance job already prints repository-wide content totals, so no extra tooling is
needed. Record the numbers now:

```console
$ kubectl -n volsync-system logs \
    "$(kubectl -n volsync-system get jobs -o name --sort-by=.metadata.creationTimestamp \
       | grep kopia-maint | tail -1)" | grep "GC found"
GC found 0 unused contents (0 B)
GC found 1606 unused contents that are too recent to delete (445.2 MB)
GC found 3899763 in-use contents (501.3 GB)
GC found 620 in-use system-contents (106.7 MB)
```

That `in-use contents` figure is the whole shared repository, every source together. It is the number
to compare against later.

#### 11b. Choose the cutoff

Any snapshot taken before step 10 finished still contains the bundles, including ones taken between
the rename in step 6 and the reclaim in step 10, because `Media.pre-migration` was still on the
volume and is the same 247G. So the cutoff is **the date of the first snapshot that shows the reduced
size**, not the date of the cutover.

The dry run in 11c prints every snapshot with its size, so read it, find the first one at roughly
44G, and use its date as `CUTOFF` in `YYYY-MM-DD` form.

#### 11c. Dry run

The Job name prefix is load-bearing: the cluster-scoped `kopia-maintenance` MutatingAdmissionPolicy
matches Jobs named `kopia-maint-*` and injects the NFS repository volume and its `/repository` mount
into the first container. That is why no NAS address appears in this manifest and why it must not be
renamed. Copy the current image pin from
`kubernetes/apps/volsync-system/kopia/app/helmrelease.yaml` rather than trusting the one below to
still be current.

`CONFIRM` is empty here, and Kopia's own `snapshot delete` is a dry run without `--delete`: it prints
`Would delete …` for each snapshot and changes nothing. That is the check that matters, because it
reports what Kopia itself resolved, not what the selection script thinks it selected.

The script selects on `--manifest-id`, and that is not cosmetic. If an argument to `snapshot delete`
is not a manifest ID, Kopia retries it as a **root object ID** and deletes every snapshot sharing
that root, and because identical trees deduplicate to the same root, that can reach snapshots
belonging to **other sources**. Manifest IDs are unique per snapshot and cannot do this. Never hand
this command an object ID.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: kopia-maint-plex-bundles-purge
  namespace: volsync-system
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
      containers:
        - name: kopia
          image: ghcr.io/home-operations/kopia:0.23.1@sha256:27da0a33b44e1b902150a6c963514237217464bb13118a95bb056667be93cda5
          command: ["/bin/sh", "-c"]
          env:
            - name: SOURCE
              value: plex-nfs@media:/data
            - name: CUTOFF
              value: "YYYY-MM-DD" # from 11b
            - name: CONFIRM
              value: "" # "--delete" only in 11d
            - name: KOPIA_CONFIG_PATH
              value: /config/repository.config
            - name: KOPIA_CACHE_DIRECTORY
              value: /cache
            - name: KOPIA_LOG_DIR
              value: /logs
            - name: HOME
              value: /tmp
          envFrom:
            - secretRef:
                name: volsync-maintenance-secret
          args:
            - |
              set -eu
              # A distinct identity, so this pod can never become the maintenance
              # owner and displace the hourly CronJob.
              kopia repository connect filesystem --path=/repository \
                --override-username=bundles-purge --override-hostname=volsync

              echo "=== every snapshot for $SOURCE"
              kopia snapshot list "$SOURCE" --all --show-identical --manifest-id

              echo "=== selecting snapshots started before $CUTOFF"
              IDS=$(kopia snapshot list "$SOURCE" --all --show-identical --manifest-id \
                | awk -v c="$CUTOFF" '$1 < c {
                    for (i = 1; i <= NF; i++)
                      if ($i ~ /^manifest:/) { sub(/^manifest:/, "", $i); print $i }
                  }')
              [ -n "$IDS" ] || { echo "nothing selected - check CUTOFF"; exit 1; }
              echo "$IDS" | wc -l
              echo "$IDS"

              echo "=== kopia snapshot delete (dry run unless CONFIRM=--delete)"
              # shellcheck disable=SC2086
              kopia snapshot delete $IDS ${CONFIRM:-}
          resources:
            requests:
              cpu: 100m
              memory: 4Gi
            limits:
              memory: 6Gi
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: { drop: ["ALL"] }
          volumeMounts:
            - name: config
              mountPath: /config
            - name: cache
              mountPath: /cache
            - name: logs
              mountPath: /logs
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: config
          emptyDir: {}
        - name: cache
          emptyDir:
            sizeLimit: 16Gi
        - name: logs
          emptyDir: {}
        - name: tmp
          emptyDir: {}
```

```bash
kubectl apply -f kopia-maint-plex-bundles-purge.yaml
kubectl -n volsync-system logs -f job/kopia-maint-plex-bundles-purge
```

Check three things in the log before going further:

- Every `Would delete …` line names `plex-nfs@media:/data`. **If any other source or any other path
  appears, stop.** Kopia resolves a source argument by walking up parent paths, so a listing is not
  guaranteed to stay on `/data` by construction. This line is the check that it did.
- The newest selected snapshot is older than the first ~44G one.
- The count is in the low hundreds, not thousands. Retention caps this source at roughly 344
  snapshots, so a four-figure count means the selection is wrong.

#### 11d. Delete

Set `CONFIRM` to `--delete`, recreate the Job, and re-read the log. `Would delete` becomes
`Deleting`.

```bash
kubectl -n volsync-system delete job kopia-maint-plex-bundles-purge
# edit CONFIRM to "--delete" in the manifest, then:
kubectl apply -f kopia-maint-plex-bundles-purge.yaml
kubectl -n volsync-system logs -f job/kopia-maint-plex-bundles-purge
kubectl -n volsync-system delete job kopia-maint-plex-bundles-purge
```

This is the point at which the bundles become genuinely unrecoverable from the NAS repository, so
confirm the result rather than assuming it. Re-run the Job one more time with `CONFIRM` set back to
empty and `CUTOFF` unchanged:

- The `=== every snapshot for …` listing should now show **only** snapshots dated on or after
  `CUTOFF`, all of them at roughly 44G.
- The selection stanza should find nothing, so the Job prints `nothing selected - check CUTOFF` and
  ends `Failed`. **Here that failure is the pass condition.** It means no pre-cutover snapshot is
  left to delete. Any other outcome means the purge was incomplete; read the listing before
  re-running 11d.

Delete the Job when finished:

```bash
kubectl -n volsync-system delete job kopia-maint-plex-bundles-purge
```

#### 11e. Wait for the space, do not chase it

**Deleting the snapshots frees nothing by itself**, and the reclaim is not fast even though
maintenance runs every hour. Snapshot deletion removes a manifest and nothing else; releasing the
underlying storage is maintenance's job, behind four separate safety gates that are all working as
intended:

| Gate | Default | Effect |
| --- | --- | --- |
| Snapshot GC runs only in the **full** cycle | n/a | Quick maintenance contributes nothing to this, however often it runs. This cluster is fine here: the mover runs quick *and* `maintenance run --full` on every hourly invocation. |
| `RequireTwoGCCycles` | true | Deleted contents are not dropped from the index until **two** successful snapshot-GC runs have happened. |
| `MarginBetweenSnapshotGC` | 4h | Those two runs must be more than four hours apart. |
| `MinContentAgeSubjectToGC` and `PackDeleteMinAge` | 24h each | Unreferenced content younger than a day is skipped, and the pack blob is kept for a further day after that. |

The hourly full cycle clears the first three gates quickly; the two 24-hour age gates are what
actually set the pace. **Expect the space back 24 to 48 hours after 11d**, not the same day.

That last gate is already visible in the log line from 11a. `GC found 1606 unused contents that are
too recent to delete` is exactly this mechanism, on content unrelated to this migration.

Do not try to hurry it. `kopia maintenance run --full` by hand will refuse anyway, because
maintenance is owned by the `maintenance@volsync` identity, and the flag that overrides the
ownership check is hidden and documented as unsafe. Lowering `--safety` is worse: upstream describes
the relaxed setting as safe only when no other Kopia clients are running, and roughly 200 sources
back up into this repository around the clock.

Re-read the same log line over the following two days:

```bash
kubectl -n volsync-system logs \
  "$(kubectl -n volsync-system get jobs -o name --sort-by=.metadata.creationTimestamp \
     | grep kopia-maint | tail -1)" | grep "GC found"
```

`in-use contents` should fall materially against the figure recorded in 11a, and the NAS dataset
holding the repository should show the corresponding drop.

For a precise number rather than a proxy, the purge Job can be re-run with its script replaced by
`kopia blob stats --raw`, which reports actual blob count and bytes on the storage. It walks every
blob in the repository over NFS, so run it deliberately, not on a loop. `kopia maintenance info` is
the other useful read-only check: its `Recent Maintenance Runs` block shows whether the
snapshot-GC task has been succeeding.

## Rollback

**Rollback A, before step 6.** Nothing has moved. Revert the pull request (or leave it merged and
keep the HelmRelease suspended), delete the `plex-bundles` PVC if it was created, resume the
HelmRelease and the Kustomization, and delete the copy Job. Plex comes back on the original layout.

**Rollback B, after step 6 and before step 10.** The original data is intact at
`Media.pre-migration`. Suspend the HelmRelease, scale the Deployment to zero, run the rename Job in
reverse (`mv "$BASE/Media.pre-migration" "$BASE/Media"`), revert the pull request so the mount is
gone from the rendered HelmRelease, then resume. Delete the `plex-bundles` PVC once Plex is healthy.
Plex returns to the original layout with every thumbnail in place.

**After step 10** there is no local rollback, but a VolSync restore of the `plex` PVC from a
pre-migration snapshot still works, which is exactly why step 9 insists on the 48-hour soak and why
the pre-flight checks confirm both backups are current. **After step 11** even that is gone: step 11
deletes the snapshots those restores would come from. Do not start it until Plex has been healthy
for the full soak.

## Follow-ups

- `VOLSYNC_CACHE_CAPACITY` for `plex` is 100Gi, sized when the source was 291G. It could come down
  once the source is 44G, but never below the 8Gi floor in
  [KB-030](../troubleshooting/kb/030-volsync-kopia-cache-pvc-too-small.md), and lowering it means
  deleting the existing cache PVCs, because those cannot shrink either. Not urgent; `miroir-local`
  is node local and cheap.
- `Metadata` at 36G is the next candidate by size, but it is a much weaker case: it holds agent
  matches, manual poster choices, and per-item edits that are **not** derivable from the media
  files. Regenerating it means re-matching the library. Leave it backed up.
