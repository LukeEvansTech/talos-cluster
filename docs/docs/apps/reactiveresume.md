# Reactive Resume

## Purpose

Reactive Resume is a resume builder, backed by CloudNativePG, Dragonfly and Garage S3, with a
bundled headless Chrome for PDF export.

## Database TLS

RR v5 bundles `pg-connection-string` v2, which forces `sslmode=require` to behave as
`verify-full` and ignores the connection string's other SSL parameters. `require`, `no-verify`,
`disable` and `uselibpqcompat=true&sslmode=require` all failed the same way, because CNPG's
server certificate is signed by the in-cluster cert-manager CA, and Node has no way to reach that
CA from the `default` namespace to trust it. `NODE_TLS_REJECT_UNAUTHORIZED=0` skips that
verification. The database hop stays TLS-encrypted, just unverified, and never leaves the
cluster's pod network. Redis and S3 traffic for this app carry no TLS at all, so the flag changes
only the database connection.
