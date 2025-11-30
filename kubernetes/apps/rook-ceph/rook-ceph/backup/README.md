# Rook-Ceph RGW S3 Backup

This directory contains the backup configuration for Rook-Ceph Object Storage (RGW).

## Overview

- **CronJob**: `rgw-s3-backup` - Backs up S3 buckets from Rook-Ceph RGW to NFS storage
- **Schedule**: Daily at 2:00 AM
- **Tool**: Uses `rclone` for S3-compatible transfers
- **Destination**: TrueNAS NFS at `/mnt/pool/backups/rgw-backups/`

## Configuration

### Backup Destination

Backups are stored on TrueNAS via NFS:
- **Server**: `${SECRET_STORAGE_SERVER}` (from cluster-secrets)
- **Path**: `/mnt/pool/backups/rgw-backups/<bucket-name>/`

### Buckets to Backup

Edit the `BUCKETS` environment variable in `rgw-backup-cronjob.yaml`:
```yaml
- name: BUCKETS
  value: "netdata"  # Space-separated list of bucket names
```

## Adding New Buckets

### 1. Create a CephObjectStoreUser

```yaml
apiVersion: ceph.rook.io/v1
kind: CephObjectStoreUser
metadata:
  name: myapp-user
  namespace: rook-ceph
spec:
  store: ceph-objectstore
  displayName: "My App User"
  capabilities:
    user: "*"
    bucket: "*"
```

### 2. Create an ObjectBucketClaim

```yaml
apiVersion: objectbucket.io/v1alpha1
kind: ObjectBucketClaim
metadata:
  name: myapp-bucket
  namespace: rook-ceph
spec:
  generateBucketName: myapp
  storageClassName: ceph-bucket
  additionalConfig:
    maxObjects: "1000000"
    maxSize: "10G"
```

### 3. Add to Backup CronJob

Add the bucket name to the `BUCKETS` env var in `rgw-backup-cronjob.yaml`.

## Access RGW

### Internal Access (from within cluster)
- **Endpoint**: `http://rook-ceph-rgw-ceph-objectstore.rook-ceph.svc.cluster.local`
- **Port**: 80

### Get S3 Credentials

**For CephObjectStoreUser:**
```bash
# Backup user
kubectl -n rook-ceph get secret rook-ceph-object-user-ceph-objectstore-backup-user \
  -o jsonpath='{.data.AccessKey}' | base64 -d

kubectl -n rook-ceph get secret rook-ceph-object-user-ceph-objectstore-backup-user \
  -o jsonpath='{.data.SecretKey}' | base64 -d
```

**For ObjectBucketClaim:**
```bash
kubectl -n rook-ceph get cm netdata-bucket -o jsonpath='{.data.BUCKET_NAME}'
kubectl -n rook-ceph get secret netdata-bucket -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d
kubectl -n rook-ceph get secret netdata-bucket -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d
```

## Testing

Run the backup job manually:
```bash
kubectl -n rook-ceph create job --from=cronjob/rgw-s3-backup rgw-s3-backup-manual-$(date +%s)
```

Check the logs:
```bash
kubectl -n rook-ceph logs -l app.kubernetes.io/name=rgw-s3-backup -f
```

## Monitoring

```bash
# CronJob status
kubectl -n rook-ceph get cronjob rgw-s3-backup

# Recent jobs
kubectl -n rook-ceph get jobs -l app.kubernetes.io/name=rgw-s3-backup
```

## Backup Structure

```
/mnt/pool/backups/rgw-backups/
├── netdata/
│   └── (synced files)
├── bucket2/
│   └── (synced files)
└── ...
```

## Notes

- Uses `rclone copy` for incremental backups (only changed files)
- Jobs are automatically cleaned up after 24 hours (TTL)
- Runs as non-root user (65534/nobody) with read-only root filesystem
- Secrets mounted as files for security compliance
