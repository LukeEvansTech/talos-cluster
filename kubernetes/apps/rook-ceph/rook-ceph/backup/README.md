# Rook-Ceph RGW S3 Backup

This directory contains the backup configuration for Rook-Ceph Object Storage (RGW).

## Overview

- **CronJob**: `rgw-s3-backup` - Backs up S3 buckets from Rook-Ceph RGW to local storage
- **Schedule**: Daily at 2:00 AM
- **Tool**: Uses `rich0/s3-sync` Docker image with s3cmd

## Setup Steps

### 1. Configure the Backup Volume

Edit `rgw-backup-cronjob.yaml` and configure the backup storage volume (line 67+):

**Option A: NFS** (recommended if you have NFS)
```yaml
volumes:
  - name: backup-storage
    nfs:
      server: your-nfs-server-ip
      path: /path/to/backup/directory
```

**Option B: HostPath** (local node storage)
```yaml
volumes:
  - name: backup-storage
    hostPath:
      path: /mnt/backup
      type: Directory
```

**Option C: PVC** (if using a PVC for backups)
```yaml
volumes:
  - name: backup-storage
    persistentVolumeClaim:
      claimName: backup-pvc
```

### 2. Set the S3 Bucket Names

Edit line 71 in `rgw-backup-cronjob.yaml` to specify which buckets to back up (space-separated):
```yaml
- name: BUCKETS
  value: "bucket1 bucket2 bucket3"  # Add all your bucket names
```

**Examples:**
- Single bucket: `value: "my-app-data"`
- Multiple buckets: `value: "app-data media-files user-uploads"`
- All buckets: Leave the script as-is (it will loop through all specified)

### 3. Create Buckets Declaratively

Buckets are created using **ObjectBucketClaim** resources (GitOps-friendly!):

**Example: Create a new bucket**

1. Create a `CephObjectStoreUser`:
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

2. Create an `ObjectBucketClaim`:
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

3. Add the bucket name to the backup CronJob's `BUCKETS` env var

**Already configured:** The `netdata` bucket is already set up declaratively! See:
- `netdata-objectstoreuser.yaml`
- `netdata-objectbucketclaim.yaml`

## Access RGW

### Internal Access (from within cluster)
- **Endpoint**: `http://rook-ceph-rgw-ceph-objectstore.rook-ceph.svc.cluster.local`
- **Port**: 80

### External Access (via Ingress)
- **URL**: `http://rgw.${SECRET_DOMAIN}` (configured as internal ingress)

### Get S3 Credentials

**For CephObjectStoreUser:**
```bash
# Backup user
kubectl -n rook-ceph get secret rook-ceph-object-user-ceph-objectstore-backup-user -o jsonpath='{.data.AccessKey}' | base64 -d
kubectl -n rook-ceph get secret rook-ceph-object-user-ceph-objectstore-backup-user -o jsonpath='{.data.SecretKey}' | base64 -d

# Netdata user
kubectl -n rook-ceph get secret rook-ceph-object-user-ceph-objectstore-netdata-user -o jsonpath='{.data.AccessKey}' | base64 -d
kubectl -n rook-ceph get secret rook-ceph-object-user-ceph-objectstore-netdata-user -o jsonpath='{.data.SecretKey}' | base64 -d
```

**For ObjectBucketClaim (includes bucket name and endpoint):**
```bash
# Get all bucket info
kubectl -n rook-ceph get cm netdata-bucket -o yaml
kubectl -n rook-ceph get secret netdata-bucket -o yaml

# Get specific values
kubectl -n rook-ceph get cm netdata-bucket -o jsonpath='{.data.BUCKET_NAME}'
kubectl -n rook-ceph get secret netdata-bucket -o jsonpath='{.data.AWS_ACCESS_KEY_ID}' | base64 -d
kubectl -n rook-ceph get secret netdata-bucket -o jsonpath='{.data.AWS_SECRET_ACCESS_KEY}' | base64 -d
```

## Testing the Backup

Run the backup job manually:
```bash
kubectl -n rook-ceph create job --from=cronjob/rgw-s3-backup rgw-s3-backup-manual-test
```

Check the logs:
```bash
kubectl -n rook-ceph logs -l app.kubernetes.io/name=rgw-s3-backup -f
```

## Monitoring

Check CronJob status:
```bash
kubectl -n rook-ceph get cronjob rgw-s3-backup
kubectl -n rook-ceph get jobs -l app.kubernetes.io/name=rgw-s3-backup
```

## Multiple Bucket Backup

The CronJob is configured to back up **multiple buckets** in a single run:

- Each bucket is synced to a separate subdirectory: `/backup/rgw-backups/<bucket-name>/`
- Buckets are processed sequentially in the order specified
- If one bucket fails, the job continues with the next bucket
- Simply add or remove bucket names from the `BUCKETS` environment variable

**Backup Structure:**
```
/backup/rgw-backups/
├── bucket1/
│   └── (synced files)
├── bucket2/
│   └── (synced files)
└── bucket3/
    └── (synced files)
```

## Notes

- The backup uses `s3cmd sync` which performs incremental backups (only changed files)
- Backup retention is managed by the CronJob history limits (1 successful, 2 failed)
- Jobs are automatically cleaned up after 24 hours (TTL)
- The backup runs after the RGW is fully deployed (dependsOn in ks.yaml)
- Each bucket gets its own subdirectory in the backup destination
