# Volsync Kopia Migration Guide

## Overview

This guide documents the migration from restic to kopia as the backup backend for volsync in the Talos cluster. The migration was completed on 2025-10-29 and affects all 35+ applications using persistent volume backups.

## Motivation

- **Modern backup tool**: Kopia is actively developed with better performance than restic
- **Community support**: perfectra1n/volsync fork provides kopia CRDs
- **Proven pattern**: Following onedr0p's home-ops implementation
- **Web UI**: Native web interface for browsing and managing snapshots
- **Better deduplication**: More efficient storage usage

## What Was Implemented

### 1. Volsync Upgrade

**Location:** `kubernetes/apps/volsync-system/volsync/app/`

**Changes:**
- Switched from official volsync chart to perfectra1n fork
- Changed from OCIRepository to HelmRepository source
- Updated to version with kopia CRDs (v0.16.13+)

**Before (restic):**
```yaml
spec:
  chart:
    spec:
      chart: app
      sourceRef:
        kind: OCIRepository
        name: volsync
```

**After (kopia):**
```yaml
spec:
  chart:
    spec:
      chart: volsync
      version: ">=0.16.0"
      sourceRef:
        kind: HelmRepository
        name: volsync-perfectra1n  # New helm repository
```

**helmrepository.yaml:**
```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: volsync-perfectra1n
spec:
  interval: 1h
  url: https://perfectra1n.github.io/volsync/charts
```

### 2. Kopia Server Deployment

**Location:** `kubernetes/apps/volsync-system/kopia-nfs/`

Deployed kopia server with web UI for NFS repository management:

**helmrelease.yaml** (following onedr0p pattern):
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: kopia-nfs
spec:
  chartRef:
    kind: OCIRepository
    name: app-template  # bjw-s app-template
  values:
    controllers:
      kopia:
        containers:
          app:
            image:
              repository: ghcr.io/home-operations/kopia
              tag: 0.21.1
            env:
              KOPIA_WEB_ENABLED: true
              KOPIA_WEB_PORT: 80
            envFrom:
              - secretRef:
                  name: kopia-nfs-secret
            args:
              - --without-password  # Passwordless web UI
    configMaps:
      config:
        data:
          repository.config: |-
            {
              "storage": {
                "type": "filesystem",
                "config": {"path": "/repository"}
              },
              "hostname": "volsync.{{ .Release.Namespace }}.svc.cluster.local",
              "username": "volsync",
              "description": "volsync",
              "enableActions": false
            }
    persistence:
      config-file:
        type: configMap
        identifier: config
        globalMounts:
          - path: /config/repository.config
            subPath: repository.config
      repository:
        type: nfs
        server: ${SECRET_STORAGE_SERVER}
        path: ${SECRET_STORAGE_SERVER_VOLSYNC_NFS}
        globalMounts:
          - path: /repository
```

**Key Configuration Points:**
- **repository.config**: Sets consistent identity (`volsync@volsync.*.svc.cluster.local`)
- **--without-password**: Enables passwordless web UI access
- **Repository path**: `/repository` (root of NFS mount)

**Web UI Access:**
- **URL**: `kopianfs.${SECRET_DOMAIN}` (configured via HTTPRoute)
- **DNSEndpoint**: Automatic DNS record via external-dns
- **Gateway**: Routes through envoy-internal

**Structure:**
```
kopia-nfs/
├── app/
│   ├── dnsendpoint.yaml      # DNS record for kopianfs.domain
│   ├── externalsecret.yaml   # Pulls KOPIA_PASSWORD from 1Password
│   ├── helmrelease.yaml      # Kopia server deployment
│   ├── httproute.yaml        # HTTPRoute for web UI access
│   ├── kustomization.yaml
│   └── ocirepository.yaml    # app-template chart reference
└── ks.yaml                    # Flux Kustomization
```

### 3. Component Templates Migration

**Location:** `kubernetes/components/volsync/nfs/`

Converted from restic to kopia backend:

**replicationsource.yaml:**
```yaml
spec:
  sourcePVC: ${APP}
  trigger:
    schedule: 0 * * * *  # Hourly backups
  kopia:  # Changed from 'restic:' to 'kopia:'
    compression: zstd-fastest
    copyMethod: ${VOLSYNC_COPYMETHOD:=Snapshot}
    parallelism: 2
    repository: ${APP}-volsync-nfs-secret
    retain:
      hourly: 24
      daily: 7
    volumeSnapshotClassName: ${VOLSYNC_SNAPSHOTCLASS:=csi-ceph-blockpool}
```

**replicationdestination.yaml:**
```yaml
spec:
  kopia:  # Changed from 'restic:' to 'kopia:'
    repository: ${APP}-volsync-nfs-secret
    sourceIdentity:
      sourceName: ${APP}-nfs  # Required for kopia restore
```

**externalsecret.yaml:**
```yaml
spec:
  target:
    template:
      data:
        KOPIA_FS_PATH: /repository  # Changed from RESTIC_*
        KOPIA_PASSWORD: "{{ .KOPIA_PASSWORD }}"
        KOPIA_REPOSITORY: filesystem:///repository
```

**Key Changes:**
- Environment variables: `RESTIC_*` → `KOPIA_*`
- Repository format: `filesystem:///repository`
- Added `sourceIdentity.sourceName` for restore operations
- Same retention policies maintained

### 4. Backup Strategy

**Current Architecture:**
```
┌─────────────────┐
│   35+ Apps      │
│   (PVCs)        │
└────────┬────────┘
         │ hourly
         ▼
┌─────────────────┐
│  Kopia/Volsync  │
│  (snapshots)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   NFS Storage   │
│  /repository    │
└────────┬────────┘
         │ nightly
         ▼
┌─────────────────┐
│  TrueNAS → R2   │
│  (offsite)      │
└─────────────────┘
```

**Backup Flow:**
1. **Hourly**: Apps → Kopia snapshots → NFS
2. **Nightly**: TrueNAS backs up NFS dataset to R2
3. **Retention**: 24 hourly, 7 daily snapshots

**Previous Strategy (abandoned during migration):**
- Initially planned dual repositories (NFS + R2)
- Simplified to NFS-only after user feedback
- TrueNAS handles offsite replication more reliably

## Migration Steps

### Step 1: Update Volsync to Kopia-Enabled Fork

1. Created HelmRepository for perfectra1n fork:
```yaml
# kubernetes/apps/volsync-system/volsync/app/helmrepository.yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: volsync-perfectra1n
spec:
  interval: 1h
  url: https://perfectra1n.github.io/volsync/charts
```

2. Updated HelmRelease to use new repository:
```yaml
spec:
  chart:
    spec:
      sourceRef:
        kind: HelmRepository
        name: volsync-perfectra1n
  values:
    fullnameOverride: volsync
    image: &image
      repository: ghcr.io/perfectra1n/volsync
      tag: v0.16.13
    kopia: *image
    manageCRDs: true
```

3. Disabled KopiaMaintenance (unstable):
```yaml
# kubernetes/apps/volsync-system/volsync/app/kustomization.yaml
resources:
  - ./helmrepository.yaml
  - ./helmrelease.yaml
  # Maintenance disabled until KopiaMaintenance CRD is stable
  # - ../maintenance
```

### Step 2: Deploy Kopia Server

1. Created kopia-nfs application structure
2. Configured repository.config with consistent identity
3. Added HTTPRoute and DNSEndpoint for web UI
4. Applied configuration:
```bash
flux reconcile kustomization cluster-apps --with-source
```

### Step 3: Migrate Component Templates

1. Updated volsync components from restic to kopia
2. Changed environment variable naming
3. Updated all 35+ app configurations automatically via component substitution

### Step 4: Verify Migration

1. Check ReplicationSources created:
```bash
kubectl get replicationsource -A | grep nfs
```

2. Verify first backups completed:
```bash
kubectl get replicationsource smokeping-nfs -n default -o yaml
```

3. Access web UI at `kopianfs.${SECRET_DOMAIN}`
4. Use "All Snapshots" dropdown to view all app snapshots

## Key Differences: Restic vs Kopia

| Aspect | Restic | Kopia |
|--------|--------|-------|
| **Environment Variables** | `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` | `KOPIA_REPOSITORY`, `KOPIA_PASSWORD` |
| **Repository Format** | `s3:https://...` or `rest:...` | `filesystem:///path` or `s3://...` |
| **Web UI** | Not available | Built-in web interface |
| **Identity** | Per-app | Configurable via repository.config |
| **CRD Field** | `spec.restic:` | `spec.kopia:` |
| **Restore** | Direct PVC reference | Requires `sourceIdentity.sourceName` |
| **Maintenance** | Manual | KopiaMaintenance CRD (when stable) |

## Web UI Usage

### Accessing the Web UI

Navigate to `kopianfs.${SECRET_DOMAIN}` in your browser.

### Viewing All Snapshots

**Important**: By default, the web UI shows only the server's own snapshots. To see backups from all applications:

1. Click the **"All Snapshots"** dropdown (top-left)
2. This reveals snapshots from all applications across all namespaces
3. Each app appears as: `{app}-nfs@{namespace}:/data`

Example snapshot identities:
- `actual-nfs@default:/data`
- `plex-nfs@media:/data`
- `ollama-nfs@ai:/data`

### Browsing Snapshots

1. Click on any snapshot path to browse files
2. View snapshot history with retention tags (hourly, daily, weekly, etc.)
3. Download or restore files directly from the UI

### Repository Information

The **Repository** tab shows:
- Total snapshots: 35+ apps × retention policy
- Storage usage and deduplication stats
- Repository path: `/repository` on NFS

## Troubleshooting

### Web UI Shows No Snapshots

**Symptom**: Web UI loads but shows empty snapshot list

**Solution**: Use the "All Snapshots" dropdown to view cross-user snapshots

**Why**: By default, kopia shows only snapshots for the current user/host identity. The server runs as `volsync@volsync.volsync-system.svc.cluster.local`, but backups are created with identities like `actual-nfs@default`.

### HTTPRoute Returns 500 Error

**Symptom**: `kopianfs.${SECRET_DOMAIN}` returns HTTP 500

**Cause**: Service name mismatch between HTTPRoute backend and actual service

**Fix**: Ensure HTTPRoute references correct service:
```yaml
# httproute.yaml
spec:
  rules:
    - backendRefs:
        - name: kopia-nfs  # Must match service name from app-template
          port: 80
```

### Repository Path Mismatch

**Symptom**: CLI shows snapshots, web UI doesn't

**Cause**: Backups going to different path than server is connected to

**Fix**: Ensure consistency:
- Server repository.config: `"path": "/repository"`
- ExternalSecret: `KOPIA_REPOSITORY: filesystem:///repository`
- Both must use same path

### Backups Failing After Migration

**Symptom**: ReplicationSource shows errors

**Check**:
```bash
kubectl describe replicationsource {app}-nfs -n {namespace}
kubectl logs -n {namespace} -l volsync.backube/replicationSource={app}-nfs
```

**Common issues**:
- Secrets not updated (still using RESTIC_* vars)
- Missing `sourceIdentity.sourceName` in ReplicationDestination
- Volume snapshot class not available

## CLI Operations

### List All Snapshots

```bash
kubectl exec -n volsync-system deployment/kopia-nfs -- \
  kopia snapshot list --all
```

### View Specific App Snapshots

```bash
kubectl exec -n volsync-system deployment/kopia-nfs -- \
  kopia snapshot list actual-nfs@default
```

### Repository Status

```bash
kubectl exec -n volsync-system deployment/kopia-nfs -- \
  kopia repository status
```

### Restore Snapshot (Manual)

```bash
kubectl exec -n volsync-system deployment/kopia-nfs -- \
  kopia snapshot restore k{snapshot-id} /restore-path
```

## 1Password Secrets

The migration reuses the existing `volsync-template` secret in 1Password:

**Required field**:
- `KOPIA_PASSWORD`: Repository encryption password

**Note**: The same password is used for both backup operations and the kopia-nfs server to connect to the repository.

## Post-Migration Status

### Deployed Resources

**volsync-system namespace**:
- 1 × kopia-nfs deployment (web UI server)
- 2 × volsync controller replicas
- 35+ × volsync-nfs-secret (one per app)

**Per-app namespace**:
- 1 × ReplicationSource (backup configuration)
- 1 × ReplicationDestination (restore configuration)
- 1 × {app}-volsync-nfs-secret (repository credentials)

### Snapshot Statistics

- **Total applications**: 35+
- **Namespaces**: default, media, downloads, ai, games, infrastructure
- **Backup frequency**: Hourly (0 * * * *)
- **Retention**: 24 hourly, 7 daily, plus weekly/monthly/annual
- **Repository size**: Tracked in web UI (Repository tab)

### Commits

Key commits from this migration:
```
b225fda1 - fix(volsync): align kopia-nfs configuration with onedr0p pattern
17c46a6b - fix(volsync): align kopia-nfs server with actual backup repository path
f06cb24e - fix(volsync): update kopia repository path to use kopia subdirectory
bc773e4e - fix(volsync): correct service name in kopia-nfs HTTPRoute
```

## Future Enhancements

### Re-enable KopiaMaintenance

Once the perfectra1n fork stabilizes the KopiaMaintenance CRD:

```yaml
# kubernetes/apps/volsync-system/volsync/maintenance/kopiamaintenance-nfs.yaml
apiVersion: volsync.backube/v1alpha1
kind: KopiaMaintenance
metadata:
  name: kopia-nfs-maintenance
spec:
  repository: kopia-nfs-secret
  schedule: "0 2 * * *"  # 2 AM daily
  operations:
    - full
    - quick
```

### Monitoring Integration

Consider adding:
- Prometheus metrics from volsync
- Grafana dashboard for backup health
- Alerts for failed backups

### Restore Testing

Periodically test restore operations:
1. Create test namespace
2. Deploy ReplicationDestination
3. Restore from snapshot
4. Verify data integrity

## References

- **onedr0p home-ops**: https://github.com/onedr0p/home-ops
- **perfectra1n/volsync**: https://github.com/perfectra1n/volsync
- **Kopia documentation**: https://kopia.io/docs/
- **Volsync documentation**: https://volsync.readthedocs.io/

## Lessons Learned

1. **Use ConfigMap for repository.config**: Ensures consistent identity across pod restarts
2. **Follow proven patterns**: onedr0p's configuration saved hours of troubleshooting
3. **Repository path must match**: Server and backup paths must be identical
4. **Web UI dropdown matters**: "All Snapshots" dropdown is crucial for cross-user visibility
5. **Simplify architecture**: NFS-only is cleaner than dual NFS+R2 when TrueNAS handles offsite
6. **Test incrementally**: Started with smokeping before migrating all 35 apps
7. **perfectra1n fork essential**: Official volsync chart doesn't have kopia CRDs yet
