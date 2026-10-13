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
   binary cannot read, so until then a rollback is a plain image revert; after it, a rollback is a
   restore from the step 1 dump.
6. Raise the FCV to `$TO` (`confirm: true` is required from 7.0 on), read it back as in step 3,
   and repeat the checks from step 5:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongosh --quiet --eval "db.adminCommand({setFeatureCompatibilityVersion: '${TO}', confirm: true})"
   ```

Moving to a minor line (8.2) is a separate FCV step too, and MongoDB stops patching a minor line
once the next one ships, so this deployment stays on the 8.0 major line.
