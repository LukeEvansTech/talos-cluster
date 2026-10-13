# Your Spotify

Spotify listening statistics in the `media` namespace: a server, a web client and a single-node
MongoDB (`yourspotify-mongodb`) whose data lives on the `yourspotify` PVC (VolSync-backed).

## MongoDB upgrades

MongoDB only upgrades one release line at a time, and each step needs the
`featureCompatibilityVersion` (FCV) raised by hand before the next one: 6.0 → 7.0 → 8.0. A binary
started on data whose FCV is two lines behind refuses to start. Two MongoDB rules in
`.renovaterc.json5` keep minor and major bumps manual, so only patches auto-merge, and give each
release line its own PR so no step gets skipped.

The image tag is a plain version such as `8.0.32`, not a distribution-suffixed one such as `6.0.4-focal`:
the suffix ties the tag to one Ubuntu release, and when upstream moved to a newer base the
suffixed tags stopped and Renovate stayed on the last one.

For each step, set the line being left and the line being entered, e.g. for 7.0 → 8.0:

```bash
FROM=7.0
TO=8.0
```

1. Dump the database to the PVC:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongodump --quiet --gzip --archive="/data/db/predump-${FROM}-$(date +%Y%m%d).archive.gz"
   ```

2. Take a VolSync backup of the PVC. Flux reverts a manual trigger on the next reconcile, so
   suspend the Kustomization, trigger the `ReplicationSource`, wait until `lastManualSync` equals
   the trigger (the mover pod sleeps up to 15 minutes of jitter first), then resume:

   ```bash
   flux suspend ks yourspotify -n media
   TRIGGER="pre-mongo-$(date +%s)"
   kubectl -n media patch replicationsource yourspotify-nfs --type merge \
     -p "{\"spec\":{\"trigger\":{\"manual\":\"$TRIGGER\"}}}"
   kubectl -n media get replicationsource yourspotify-nfs \
     -o jsonpath='{.status.lastManualSync} {.status.latestMoverStatus.result}'
   flux resume ks yourspotify -n media
   ```

3. Confirm the FCV is `$FROM`:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongosh --quiet --eval 'db.adminCommand({getParameter: 1, featureCompatibilityVersion: 1})'
   ```

4. Merge the image bump to the `$TO` line and wait for the rollout.
5. Raise the FCV to `$TO` (`confirm: true` is required from 7.0 on), then read it back as in
   step 3:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongosh --quiet --eval "db.adminCommand({setFeatureCompatibilityVersion: '${TO}', confirm: true})"
   ```

6. Check the server logs and that the API route answers before the next step.

Moving to a minor line (8.2) is a separate FCV step too, and MongoDB stops patching a minor line
once the next one ships, so this deployment stays on the 8.0 major line.
