# Reactive Resume

## Purpose

Reactive Resume is a resume builder, backed by CloudNativePG, Dragonfly and Garage S3. The app
renders PDFs itself, so there is no printer or browser sidecar.

## v6 upgrade

v6 moved the image to `ghcr.io/reactive-resume/reactive-resume`. Its startup migration drops
columns v5 reads, so the controller uses the `Recreate` strategy and a v5 pod must never start
against the migrated database. `STRICT_SCHEMA_CHECK` makes a failed migration fail the pod rather
than surface later as request errors. The rollback path is a restore of the pre-upgrade backup;
upstream's `rollback.sql` leaves the migration recorded as applied (see the
[upstream guide](https://docs.rxresu.me/self-hosting/upgrading-to-v6)).

## Database TLS

RR v5 bundles `pg-connection-string` v2, which forces `sslmode=require` to behave as
`verify-full` and ignores the connection string's other SSL parameters. `require`, `no-verify`,
`disable` and `uselibpqcompat=true&sslmode=require` all failed the same way, because CNPG's
server certificate is signed by the in-cluster cert-manager CA, and Node has no way to reach that
CA from the `default` namespace to trust it. `NODE_TLS_REJECT_UNAUTHORIZED=0` skips that
verification. The database hop stays TLS-encrypted, just unverified, and never leaves the
cluster's pod network. Redis and S3 traffic for this app carry no TLS at all, so the flag changes
only the database connection.
