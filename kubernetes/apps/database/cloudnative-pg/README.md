# CloudNative-PG PostgreSQL 18 Implementation

## Overview

This directory contains a complete CloudNative-PG deployment for PostgreSQL 18 with high availability, dual backup strategies, comprehensive monitoring, and connection pooling.

## Architecture

### Directory Structure

```
cloudnative-pg/
├── README.md                 # This file
├── ks.yaml                   # Flux Kustomizations (deployment orchestration)
├── app/                      # CloudNative-PG Operator
│   ├── externalsecret.yaml   # 1Password integration for credentials
│   ├── helmrelease.yaml      # Operator deployment (v0.23.0)
│   ├── ocirepository.yaml    # OCI source for operator chart
│   └── kustomization.yaml
├── cluster/                  # PostgreSQL 18 Cluster
│   ├── cluster.yaml          # Main cluster config (3 instances, 20Gi, PG18)
│   ├── scheduledbackup.yaml  # Daily Barman Cloud S3 backups
│   ├── pooler.yaml           # pgBouncer connection pooling
│   ├── podmonitor.yaml       # Prometheus metrics collection
│   ├── prometheusrule.yaml   # 7 critical alerts
│   ├── gatus.yaml            # TCP health checks
│   ├── service.yaml          # LoadBalancer at 192.168.222.18
│   └── kustomization.yaml
└── backup/                   # NFS Database Dumps
    ├── helmrelease.yaml      # Daily cronjob (2 AM)
    ├── ocirepository.yaml    # bjw-s app-template chart
    └── kustomization.yaml
```

## Design Decisions

### PostgreSQL Configuration

- **Version**: PostgreSQL 18.0 (latest stable)
- **Instances**: 3 for high availability with automatic failover
- **Storage**: 20Gi using `openebs-hostpath`
- **Resources**:
  - Requests: 500m CPU, 2Gi memory
  - Limits: 4Gi memory
- **Tuning**: Optimized for mixed workloads
  - `max_connections: 300`
  - `shared_buffers: 512MB`
  - `effective_cache_size: 1536MB`
  - Transaction-level checkpoint completion

### Backup Strategy

We implemented a **dual backup approach** for redundancy:

#### 1. Barman Cloud (S3/MinIO) - Primary
- **Method**: Continuous WAL archiving via Barman Object Store
- **Schedule**: Daily scheduled backups via `ScheduledBackup` CR
- **Retention**: 30 days
- **Compression**: bzip2 for both WAL and data
- **Performance**: 8 parallel WAL transfers, 2 parallel data jobs
- **Location**: `s3://cloudnative-pg/` at `https://${SECRET_STORAGE_SERVER}:9000`
- **Use Case**: Point-in-time recovery (PITR), cluster bootstrap/migration

#### 2. NFS Database Dumps - Secondary
- **Method**: `pg_dump` via tiredofit/docker-db-backup
- **Schedule**: Daily at 2 AM (cronjob)
- **Retention**: 7 days (10080 minutes)
- **Compression**: gzip level 9
- **Location**: NAS via NFS mount at `/backups/database/postgresql`
- **Use Case**: Simple database-level restores, offsite backups

**Why Both?**
- Barman provides PITR and cluster-level recovery
- NFS dumps provide simple SQL-level restores and additional redundancy
- Different failure domains (object storage vs. file storage)

### Connection Pooling

- **Pooler**: pgBouncer (3 instances for HA)
- **Mode**: Transaction-level pooling
- **Capacity**: 1000 max client connections, 100 default pool size
- **Monitoring**: PodMonitor enabled for metrics

### Monitoring Stack

#### Metrics Collection
- **PodMonitor**: Scrapes PostgreSQL metrics from all instances
- **Metrics Port**: Standard CNPG metrics endpoint
- **Label Relabeling**: Converts `cluster` → `cnpg_cluster` for clarity

#### Alerting (PrometheusRule)
Seven critical alerts configured:

1. **LongRunningTransaction**: Transactions > 5 minutes
2. **BackendsWaiting**: Backends waiting > 5 minutes
3. **PGDatabase**: Transaction age > 300M (vacuum/wraparound risk)
4. **PGReplication**: Replication lag > 5 minutes
5. **LastFailedArchiveTime**: WAL archiving failures
6. **DatabaseDeadlockConflicts**: > 10 deadlocks detected
7. **ReplicaFailingReplication**: Replica replication failures

#### Health Checks
- **Gatus**: TCP connectivity checks every minute
- **Endpoint**: `postgres18-rw.database.svc.cluster.local:5432`
- **Alerts**: Gotify notifications on failure

#### Dashboards
- **Grafana**: Auto-deployed with operator (`grafanaDashboard.create: true`)

### External Access

- **Service Type**: LoadBalancer
- **IP**: `${SVC_POSTGRES_ADDR}` (resolves to `10.32.8.91` via cluster-secrets)
- **DNS**: `postgres18.${SECRET_DOMAIN_INT}` (External DNS)
- **Target**: Primary instance only (read-write)

## Deployment Flow

Flux will deploy components in this order (via `dependsOn`):

```
1. external-secrets-onepassword (pre-existing)
   ↓
2. cloudnative-pg (operator)
   ↓ (also depends on openebs)
3. cloudnative-pg-cluster (PostgreSQL 18)
   ↓
4. cloudnative-pg-backup (NFS cronjob)
```

Each Flux Kustomization has:
- 1h reconciliation interval
- 2m retry interval
- 5m timeout
- Health checks on the Cluster CR

## Current Status

### ✅ Completed

- [x] Operator deployment configuration
- [x] PostgreSQL 18 cluster manifests (3 instances)
- [x] Barman Cloud S3 backup configuration
- [x] NFS backup cronjob setup
- [x] PodMonitor for metrics
- [x] PrometheusRule with 7 alerts
- [x] Gatus health checks
- [x] pgBouncer connection pooling
- [x] LoadBalancer service configuration
- [x] Flux Kustomizations with dependency chain
- [x] All manifests validated with `kubectl kustomize`

### ⚠️ Pre-Deployment Requirements

Before merging/deploying, you **must** complete:

#### 1. 1Password Configuration

Create a vault item named **`cloudnative-pg`** with:

```
POSTGRES_SUPER_USER: postgres
POSTGRES_SUPER_PASS: <generate-strong-password>
```

**Note**: MinIO credentials are pulled from the existing `minio` vault item:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

#### 2. NFS Backup Configuration

Edit `backup/helmrelease.yaml` lines 105-106:

```yaml
backups:
  type: nfs
  server: nas01.${SECRET_DOMAIN_INT}  # TODO: Replace with your NAS
  path: /mnt/data/backup               # TODO: Replace with your NFS path
```

**Required**:
- NFS server must be accessible from the cluster
- Export path must exist and be writable
- Verify with: `showmount -e <nas-server>`

#### 3. MinIO S3 Bucket

Create the S3 bucket in MinIO:

```bash
# Using mc (MinIO Client)
mc mb minio/cloudnative-pg

# Or via MinIO UI
# Navigate to https://${SECRET_STORAGE_SERVER}:9000
# Create bucket: cloudnative-pg
```

Verify credentials in the `minio` 1Password vault have access.

## Post-Deployment Verification

### Check Operator

```bash
kubectl get pods -n database -l app.kubernetes.io/name=cloudnative-pg
# Expected: 2 operator pods (replicaCount: 2)
```

### Check Cluster

```bash
# Cluster status
kubectl get cluster -n database postgres18
# Expected: STATUS=Cluster in healthy state (3/3)

# Pod status
kubectl get pods -n database -l cnpg.io/cluster=postgres18
# Expected: 3 pods running (postgres18-1, postgres18-2, postgres18-3)

# Check which is primary
kubectl get cluster -n database postgres18 -o jsonpath='{.status.currentPrimary}'
```

### Check Pooler

```bash
kubectl get pooler -n database
kubectl get pods -n database -l cnpg.io/pooler=postgres18-pgbouncer-rw
# Expected: 3 pgbouncer pods
```

### Check Backups

```bash
# Scheduled backup
kubectl get scheduledbackup -n database
# Expected: postgres18 (next run time shown)

# Actual backups
kubectl get backup -n database
# Expected: After first daily run, backups listed

# NFS backup cronjob
kubectl get cronjob -n database
# Expected: postgres18-backup-postgres-backup
```

### Test Connectivity

#### Internal (via service)

```bash
kubectl run -it --rm psql --image=postgres:18 --restart=Never -- \
  psql -h postgres18-rw.database.svc.cluster.local -U postgres -d postgres
```

#### External (via LoadBalancer)

```bash
psql -h 10.32.8.91 -U postgres -d postgres
# Or via DNS
psql -h postgres18.${SECRET_DOMAIN_INT} -U postgres -d postgres
```

### Check Monitoring

```bash
# Prometheus metrics
kubectl port-forward -n database postgres18-1 9187:9187
curl localhost:9187/metrics | grep cnpg

# Gatus status
# Check your Gatus dashboard for postgres18 endpoint

# Prometheus alerts
# Check AlertManager for any firing CloudNative-PG alerts
```

## Accessing the Database

### Connection Strings

#### Read-Write (Primary)

```bash
# Via service
postgres://postgres:<password>@postgres18-rw.database.svc.cluster.local:5432/postgres

# Via LoadBalancer
postgres://postgres:<password>@192.168.222.18:5432/postgres

# Via pooler (recommended for apps)
postgres://postgres:<password>@postgres18-pgbouncer-rw.database.svc.cluster.local:5432/postgres
```

#### Read-Only (Replicas)

```bash
postgres://postgres:<password>@postgres18-ro.database.svc.cluster.local:5432/postgres
```

### Creating Databases/Users

```bash
# Connect to primary
kubectl exec -it -n database postgres18-1 -- psql -U postgres

# Create database
CREATE DATABASE myapp;

# Create user
CREATE USER myapp_user WITH PASSWORD 'secure_password';
GRANT ALL PRIVILEGES ON DATABASE myapp TO myapp_user;
```

## Backup & Recovery

### Manual Backup (Barman)

```bash
# Trigger backup immediately
kubectl create -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: postgres18-manual-$(date +%Y%m%d-%H%M%S)
  namespace: database
spec:
  cluster:
    name: postgres18
  method: barmanObjectStore
EOF

# Check backup status
kubectl get backup -n database
```

### Restore from Barman Backup

To restore from a backup, modify `cluster/cluster.yaml`:

```yaml
spec:
  bootstrap:
    recovery:
      source: postgres18
      recoveryTarget:
        targetTime: "2025-11-13 12:00:00.00000+00"  # PITR
  externalClusters:
    - name: postgres18
      barmanObjectStore:
        destinationPath: s3://cloudnative-pg/
        endpointURL: https://${SECRET_STORAGE_SERVER}:9000
        s3Credentials:
          accessKeyId:
            name: cloudnative-pg-secret
            key: AWS_ACCESS_KEY_ID
          secretAccessKey:
            name: cloudnative-pg-secret
            key: AWS_SECRET_ACCESS_KEY
        wal:
          compression: bzip2
```

### Restore from NFS Dump

```bash
# List available backups on NAS
ls -lh /mnt/data/backup/database/postgresql/

# Restore specific database
gunzip < /mnt/data/backup/database/postgresql/mydb_YYYYMMDD.sql.gz | \
  kubectl exec -i postgres18-1 -n database -- psql -U postgres -d mydb
```

## Troubleshooting

### Cluster Won't Start

```bash
# Check operator logs
kubectl logs -n database -l app.kubernetes.io/name=cloudnative-pg

# Check cluster events
kubectl describe cluster -n database postgres18

# Check pod logs
kubectl logs -n database postgres18-1
```

### Backup Failures

```bash
# Check scheduled backup
kubectl describe scheduledbackup -n database postgres18

# Check backup CR events
kubectl get backup -n database
kubectl describe backup -n database <backup-name>

# Verify S3 credentials
kubectl get secret -n database cloudnative-pg-secret -o yaml
```

### Replication Issues

```bash
# Check replication status
kubectl exec -n database postgres18-1 -- psql -U postgres -c \
  "SELECT * FROM pg_stat_replication;"

# Check cluster status
kubectl get cluster -n database postgres18 -o yaml
```

## Maintenance

### Upgrading PostgreSQL

CloudNative-PG supports in-place major version upgrades:

1. Update `imageName` in `cluster/cluster.yaml`
2. Set `primaryUpdateStrategy: unsupervised` (already set)
3. Apply changes - operator handles rolling upgrade

### Scaling Instances

```bash
# Edit cluster
kubectl edit cluster -n database postgres18

# Change instances: 3 -> 5
spec:
  instances: 5
```

### Vacuuming

CloudNative-PG includes autovacuum by default. For manual vacuum:

```bash
kubectl exec -n database postgres18-1 -- psql -U postgres -d mydb -c "VACUUM ANALYZE;"
```

## References

### Implementation Sources

This implementation is based on:
- [drag0n141/home-ops](https://github.com/drag0n141/home-ops/tree/master/kubernetes/apps/database/cloudnative-pg) - Primary reference for cluster config
- [jfroy/flatops](https://github.com/jfroy/flatops/tree/main/kubernetes/apps/database/cnpg) - PodMonitor configuration
- Previous implementation: [talos-cluster-pw](https://github.com/LukeEvansTech/talos-cluster-pw/tree/main/kubernetes/apps/database/cloudnative-pg)

### Documentation

- [CloudNative-PG Official Docs](https://cloudnative-pg.io/)
- [CloudNative-PG GitHub](https://github.com/cloudnative-pg/cloudnative-pg)
- [Backup & Recovery Guide](https://cloudnative-pg.io/documentation/current/backup_recovery/)
- [Monitoring Guide](https://cloudnative-pg.io/documentation/current/monitoring/)

## Notes

- **Storage Class**: Using `openebs-hostpath` - data is local to nodes
- **Network Policy**: Not implemented - assume cluster network isolation
- **TLS**: Not configured - rely on cluster network security (consider adding for production)
- **Resource Limits**: Conservative - adjust based on workload monitoring
- **Timezone**: Backup cronjob uses `America/New_York`

## Next Steps

After successful deployment:

1. Monitor cluster health for 24h
2. Verify first backup completes successfully (S3 + NFS)
3. Test restore procedure in non-prod namespace
4. Create application databases/users
5. Update app configurations to use pooler endpoint
6. Consider adding TLS for external connections
7. Tune PostgreSQL parameters based on workload
8. Set up backup restore testing schedule
