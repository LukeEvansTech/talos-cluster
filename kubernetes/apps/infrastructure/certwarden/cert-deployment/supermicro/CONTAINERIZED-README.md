# Supermicro Certificate Deployment - Containerized Approach

## Current Setup (ACTIVE)

This deployment now uses a **pre-built container** for faster and more reliable certificate deployment:

- **Container**: `ghcr.io/lukeevanstech/supermicro-ipmi-cert:latest`
- **Script**: `certwarden-supermicro-deploy.sh`
- **Benefits**:
  - Faster execution (no runtime dependency installation)
  - Consistent environment across deployments
  - Reduced job execution time by ~30-60 seconds
  - Better security (minimal container, no runtime downloads)

## Files

- **certwarden-supermicro-deploy.sh** - Active script (containerized approach)
- **certwarden-supermicro-deploy.sh.OLD** - Backup of old script (runtime installation)
- **supermicro-updater.py** - Python script (kept for reference, baked into container)

## How It Works

1. Certwarden calls `certwarden-supermicro-deploy.sh` after certificate renewal
2. Script creates a Kubernetes Job using the pre-built container
3. Container has all dependencies pre-installed:
   - kubectl v1.32.0
   - Python 3.12 + requests + pyOpenSSL
   - supermicro_ipmi_cert.py script
4. Job deploys certificate via Redfish API
5. Job auto-cleans up after 5 minutes

## Testing

Test the deployment through the Certwarden UI:
1. Go to Certwarden UI
2. Select a Supermicro certificate
3. Click "Renew" or trigger post-processing
4. Monitor the job: `kubectl get jobs -n infrastructure -l app.kubernetes.io/name=certwarden-supermicro-deploy -w`
5. Check logs: `kubectl logs -n infrastructure job/<job-name>`

## Rollback to Old Method

If you need to rollback to the old runtime-installation method:

1. Edit `kustomization.yaml`:
   ```yaml
   # Comment out containerized approach:
   # - name: certwarden-supermicro-scripts
   #   files:
   #     - certwarden-supermicro-deploy.sh

   # Uncomment old approach:
   - name: certwarden-supermicro-scripts
     files:
       - supermicro-updater.py
       - certwarden-supermicro-deploy.sh.OLD
   ```

2. Apply changes:
   ```bash
   kubectl delete configmap certwarden-supermicro-scripts -n infrastructure
   kubectl apply -k .
   ```

3. Restart Certwarden to pick up the new ConfigMap

## Container Source

The container is built from: `https://github.com/LukeEvansTech/containers/tree/main/apps/supermicro-ipmi-cert`

Updates are automatically built and pushed to GHCR when changes are committed to the main branch.

## Troubleshooting

**Job fails with ImagePullBackOff:**
- Check container registry: `docker pull ghcr.io/lukeevanstech/supermicro-ipmi-cert:latest`
- Verify GHCR is accessible from cluster

**Job fails with "kubectl not found":**
- Container should have kubectl pre-installed at `/usr/local/bin/kubectl`
- Check container build logs in GitHub Actions

**Certificate upload fails:**
- Same troubleshooting as old method
- Check IPMI credentials in ExternalSecret
- Verify Redfish API is enabled on IPMI
- Check network connectivity from cluster to IPMI

## Supported Models

- Supermicro X12 series (Redfish)
- Supermicro X13 series (Redfish)
- Supermicro H13 series (Redfish)

Note: X11 and older models are NOT supported (legacy IPMI interface removed).
