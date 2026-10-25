# Your Spotify

Spotify listening statistics in the `media` namespace: a server, a web client and a single-node
MongoDB (`yourspotify-mongodb`) whose data lives on the `yourspotify` PVC (VolSync-backed).

## MongoDB upgrades

MongoDB only upgrades one release line at a time, and each step needs the
`featureCompatibilityVersion` (FCV) raised by hand before the next one: 6.0 → 7.0 → 8.0 → 9.0.
A binary started on data whose FCV is two lines behind refuses to start; 9.0 starts only on FCV
8.0 or 8.3. MongoDB rules in
`.renovaterc.json5` keep minor and major bumps manual, so only patches auto-merge, give each
release line its own PR so no step gets skipped, and offer only major lines (`X.0.y`), never the
8.2-style minor track.

The image tag is a plain version such as `8.0.32`, not a distribution-suffixed one such as `6.0.4-focal`:
the suffix ties the tag to one Ubuntu release, and when upstream moved to a newer base the
suffixed tags stopped and Renovate stayed on the last one.

For each step, set the line being left and the line being entered, e.g. for 8.0 → 9.0:

```bash
FROM=8.0
TO=9.0
```

1. Dump the database to the PVC:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongodump --quiet --gzip --archive="/data/db/predump-${FROM}-$(date +%Y%m%d).archive.gz"
   ```

2. Take a VolSync backup of the PVC. Flux reverts a manual trigger on the next reconcile, so
   suspend the Kustomization, trigger the `ReplicationSource`, wait until `lastManualSync` equals
   the trigger (the mover pod sleeps up to 15 minutes of jitter first), then resume:

   A run already in progress when the trigger changes still completes as the manual sync and
   may predate the dump, so wait for it before triggering:

   ```bash
   flux suspend ks yourspotify -n media
   until [ "$(kubectl -n media get replicationsource yourspotify-nfs \
     -o jsonpath='{.status.conditions[?(@.type=="Synchronizing")].status}')" = "False" ]; do sleep 10; done
   TRIGGER="pre-mongo-$(date +%s)"
   kubectl -n media patch replicationsource yourspotify-nfs --type merge \
     -p "{\"spec\":{\"trigger\":{\"manual\":\"$TRIGGER\"}}}"
   until [ "$(kubectl -n media get replicationsource yourspotify-nfs \
     -o jsonpath='{.status.lastManualSync}')" = "$TRIGGER" ]; do sleep 10; done
   kubectl -n media get replicationsource yourspotify-nfs \
     -o jsonpath='{.status.latestMoverStatus.result}'
   ```

   Go on only if that prints `Successful`, then `flux resume ks yourspotify -n media`.

3. Confirm the FCV is `$FROM`:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongosh --quiet --eval 'db.adminCommand({getParameter: 1, featureCompatibilityVersion: 1})'
   ```

4. Merge the image bump to the `$TO` line and wait for the rollout.
5. Burn in the new binary at the old FCV: the server logs are clean, the API route answers, and
   it runs a day of normal use before step 6. Raising the FCV enables features the previous
   binary cannot read, so until then a rollback is an image revert; after it, lower the FCV first
   or restore from the step 1 dump or the step 2 VolSync backup. For 9.0, follow "9.0 rollback"
   below, which also removes a 9.0-only startup argument.
6. Raise the FCV to `$TO` (`confirm: true` is required from 7.0 on), read it back as in step 3,
   and repeat the checks from step 5:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongosh --quiet --eval "db.adminCommand({setFeatureCompatibilityVersion: '${TO}', confirm: true})"
   ```

Moving to a minor line (8.2, 8.3) is a separate FCV step too, and MongoDB stops patching a minor
line once the next one ships, so this deployment follows major lines only (`X.0.y`) and the
Renovate `allowedVersions` rule offers nothing else.

## 9.0 rollback

MongoDB supports a direct 9.0 → 8.0 downgrade for a standalone
([procedure](https://www.mongodb.com/docs/manual/release-notes/9.0-downgrade-to-8.0-standalone/)),
so the 8.3 minor line is never needed as an escape tag:

- While the FCV is still 8.0, revert the image tag to the last 8.0.y **and** remove the mongodb
  container's `args` block in the same commit: `internalQueryMaxMemoryUsageBytesPerOperation` is a
  9.0-only parameter, and 8.0 exits on an unknown `--setParameter`, so the pod would crash-loop.
- After the FCV is raised to 9.0, first lower it with
  `setFeatureCompatibilityVersion: '8.0', confirm: true`, remove any 9.0-only features, then revert
  the image and drop the `args` block as above.
- If that fails, restore the PVC from the pre-upgrade VolSync backup, or `mongorestore` the step 1
  dump into an empty 8.0 data directory.

9.0.4 also moves the image base from Ubuntu 24.04 to 26.04; `mongosh` and `mongodump` are still
in it.

## 9.0 notes

- The mongod container passes `internalQueryMaxMemoryUsageBytesPerOperation=134217728` (128 MiB,
  which with the 256 MB WiredTiger cache stays under the limit). 9.0 caps
  a single query at 1 GB or 20% of memory, whichever is greater, which is above the 512Mi container
  limit, so without it a runaway query would OOM-kill the pod instead of failing with
  `ExceededMemoryLimit`. If the stats views start failing on long date ranges, raise it first;
  `metrics.query.operationsFailedDueToMemoryLimit` in `serverStatus` counts those failures.
- your_spotify 1.20.0 is validated upstream only up to MongoDB 8.3; 9.0 is an untested pairing, which
  is why the burn-in below matters.
- At FCV 9.0, `listIndexes` reports `collation: { locale: "simple" }`
  ([SERVER-89953](https://jira.mongodb.org/browse/SERVER-89953)), and Mongoose releases before
  9.10.4 rebuild such indexes in `syncIndexes()`. your_spotify does not call `syncIndexes()`, so a
  rebuild is not expected; if one shows in the logs it is benign.

## Follow-up: raise the FCV to 9.0

The 9.0.4 binary went live on FCV 8.0 (2026-10-20). After at least a day of clean burn-in, run steps 5
and 6 above with `TO=9.0`. Until then a rollback is an image revert plus removing the 9.0-only
`args` (see "9.0 rollback" above).

## Importing streaming history

The "Extended streaming history" import looks up every track it has not seen before, as fast as it
can. A full export (about 255k plays here) gets the app throttled by Spotify, with `Retry-After`
values from minutes to a day. Since 1.20.0 the server holds every Spotify call behind one
process-wide gate until that time passes, logged only at debug level. Startup, the 2-minute sync and
the web login all wait silently behind it (upstream issues
[#619](https://github.com/Yooooomi/your_spotify/issues/619) and
[#627](https://github.com/Yooooomi/your_spotify/issues/627)).

The deployment is shaped around that:

- Helm does not wait for the server to become ready, so a rollout during a throttle is not
  rolled back.
- The server has a readiness probe only. A liveness or startup probe killed it while it waited,
  and each restart's first Spotify call re-armed the throttle (2026-10-25).
- Uploaded files land on the `yourspotify-imports` PVC at `/tmp/imports`, not on the
  `/tmp` emptyDir, so a restart no longer deletes them and a failed import can be retried.

To run an import:

1. Upload a few files at a time, not the whole export; once one batch finishes, add the next.
2. Leave the server alone while it runs. A stall with no new log lines is the throttle; it resumes
   by itself when the wait ends. Restarting the pod marks the import failed.
3. Check progress in MongoDB. This shows how far the import got, not how long the throttle
   lasts: that wait lives only in the server's memory and is logged only at debug level
   (`Delaying request for Xms due to Retry-After`).

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- mongosh --quiet your_spotify \
     --eval 'db.importerstates.find({}, {status: 1, current: 1, total: 1, updatedAt: 1})'
   ```

Re-uploading a file already imported skips its duplicate plays, but its tracks are looked up again,
which spends the same Spotify budget.
