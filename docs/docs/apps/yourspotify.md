# Your Spotify

Spotify listening statistics in the `media` namespace: a server, a web client and a single-node
MongoDB (`yourspotify-mongodb`) whose data lives on the `yourspotify` PVC (VolSync-backed).

## MongoDB upgrades

MongoDB only upgrades one release line at a time, and each step needs the
`featureCompatibilityVersion` (FCV) raised by hand before the next one: 6.0 → 7.0 → 8.0. A binary
started on data whose FCV is two lines behind refuses to start. The Renovate rule
"MongoDB: manual review for minor and major lines" in `.renovaterc.json5` keeps those bumps
manual, so only patches auto-merge.

The image tag is a plain version (`8.0.32`), not a distribution-suffixed one such as `6.0.4-focal`:
the suffix ties the tag to one Ubuntu release, and when upstream moved to a newer base the
suffixed tags stopped and Renovate stayed on the last one.

For each step:

1. Take a dump to the PVC and a VolSync snapshot (suspend the Kustomization first, see
   [backups](../operations/backups.md#restoring)):

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongodump --quiet --gzip --archive=/data/db/predump-<version>-<date>.archive.gz
   ```

2. Confirm the FCV matches the running line:

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongosh --quiet --eval 'db.adminCommand({getParameter: 1, featureCompatibilityVersion: 1})'
   ```

3. Merge the image bump for the next line and wait for the rollout.
4. Raise the FCV to the new line (`confirm: true` is required from 7.0 on):

   ```bash
   kubectl -n media exec deploy/yourspotify-mongodb -- \
     mongosh --quiet --eval 'db.adminCommand({setFeatureCompatibilityVersion: "7.0", confirm: true})'
   ```

5. Check the server logs and that the API route answers before the next step.

Moving to a minor line (8.2) is a separate FCV step too, and MongoDB stops patching a minor line
once the next one ships, so this deployment stays on the 8.0 major line.
