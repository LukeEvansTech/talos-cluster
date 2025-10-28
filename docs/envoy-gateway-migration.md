# Envoy Gateway Migration Guide

## Overview

This guide documents the implementation of Envoy Gateway v1.5.4 in the Talos cluster and provides instructions for migrating applications from nginx ingress to Gateway API HTTPRoutes.

## What Was Implemented

### 1. Envoy Gateway Deployment

**Location:** `kubernetes/apps/network/envoy-gateway/`

- **Chart:** `oci://mirror.gcr.io/envoyproxy/gateway-helm v1.5.4`
- **CRDs:** Installed via `scripts/bootstrap-apps.sh` for GitOps compatibility
- **Components:**
  - 1 Envoy Gateway controller pod
  - 2 external proxy replicas
  - 2 internal proxy replicas

### 2. Certificate Management

**Location:** `kubernetes/apps/network/certificates/`

Created certificate import structure following onedr0p's pattern:

```yaml
network/
├── certificates/
│   ├── import/
│   │   ├── externalsecret.yaml  # Pulls wildcard cert from 1Password
│   │   └── kustomization.yaml
│   └── ks.yaml
```

**Key Points:**
- Wildcard certificate (`*.${SECRET_DOMAIN}`) imported from 1Password
- Certificate stored in `network` namespace (same as Gateway)
- No cross-namespace certificate references needed
- Existing `cert-manager/tls/` setup remains for nginx compatibility

### 3. Gateway Resources

**Location:** `kubernetes/apps/network/envoy-gateway/app/envoy.yaml`

Two Gateway instances configured:

| Gateway | IP | Type | Purpose |
|---------|------------|----------|---------|
| envoy-external | 10.32.8.89 | external | Public-facing services |
| envoy-internal | 10.32.8.90 | internal | Internal-only services |

**Features Enabled:**
- HTTP/3 support
- Brotli & Gzip compression
- TCP keepalive
- TLS 1.2+ with ALPN (h2, http/1.1)
- Advanced buffer management
- X-Forwarded-For client IP detection

### 4. IP Allocation Strategy

**Current Allocation (Phased Migration):**
- nginx external: `10.32.8.88` (existing)
- nginx internal: `10.32.8.87` (existing)
- **Envoy external: `10.32.8.89`** (new)
- **Envoy internal: `10.32.8.90`** (new)

**Post-Migration Plan:**
When ready to decommission nginx, update Envoy Gateway IPs to:
- Envoy external: `10.32.8.88`
- Envoy internal: `10.32.8.87`

## Migrating Applications to Envoy Gateway

### Step 1: Create HTTPRoute

Create an HTTPRoute resource in your application directory:

**Example:** `kubernetes/apps/media/pinchflat/app/httproute.yaml`

```yaml
---
# yaml-language-server: $schema=https://kubernetes-schemas.pages.dev/gateway.networking.k8s.io/httproute_v1.json
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: pinchflat
spec:
  parentRefs:
    - name: envoy-internal  # or envoy-external for public apps
      namespace: network
      sectionName: https
  hostnames:
    - "pinchflat.${SECRET_DOMAIN}"
    - "pinchflat.${SECRET_INTERNAL_DOMAIN}"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: pinchflat  # Service name
          port: 80         # Service port
```

### Step 2: Update Kustomization

Add the HTTPRoute to your app's kustomization:

**Example:** `kubernetes/apps/media/pinchflat/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ./helmrelease.yaml
  - ./httproute.yaml  # Add this line
```

### Step 3: Choose Gateway Type

**Internal Gateway** (`envoy-internal`):
- Use for services only accessible within your network
- Examples: Home automation, internal tools, private apps

**External Gateway** (`envoy-external`):
- Use for publicly accessible services
- Examples: Public websites, external APIs

### Step 4: Test the HTTPRoute

```bash
# Reconcile Flux
flux reconcile source git flux-system
flux reconcile kustomization cluster-apps

# Check HTTPRoute status
kubectl get httproute <name> -n <namespace>

# Test from within cluster
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -I -k -H "Host: <hostname>" https://<gateway-ip>
```

### Step 5: Verify and Monitor

```bash
# Check Gateway status
kubectl get gateway -n network

# Check HTTPRoute acceptance
kubectl describe httproute <name> -n <namespace>

# View Envoy Gateway logs
kubectl logs -n envoy-gateway-system -l app.kubernetes.io/name=envoy-gateway

# View proxy logs
kubectl logs -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=envoy-internal
```

## Common HTTPRoute Patterns

### Path-Based Routing

```yaml
rules:
  - matches:
      - path:
          type: PathPrefix
          value: /api
    backendRefs:
      - name: api-service
        port: 8080
  - matches:
      - path:
          type: PathPrefix
          value: /
    backendRefs:
      - name: web-service
        port: 80
```

### Header-Based Routing

```yaml
rules:
  - matches:
      - headers:
          - name: X-Version
            value: v2
    backendRefs:
      - name: app-v2
        port: 80
```

### Multiple Hostnames

```yaml
hostnames:
  - "app.${SECRET_DOMAIN}"
  - "app.${SECRET_INTERNAL_DOMAIN}"
  - "legacy-app.${SECRET_DOMAIN}"
```

## External-DNS Integration

External-DNS is configured to automatically create DNS records for HTTPRoutes attached to the external Gateway:

```yaml
# external-dns configuration (already configured)
extraArgs:
  - --gateway-label-filter=type=external
sources:
  - "gateway-httproute"
```

**How it works:**
- HTTPRoutes attached to `envoy-external` Gateway automatically get DNS records
- Uses `external-dns.alpha.kubernetes.io/target` annotation from Gateway
- No additional annotations needed on HTTPRoutes

## Migration Checklist

- [ ] Create HTTPRoute YAML file
- [ ] Add HTTPRoute to app kustomization
- [ ] Commit and push changes
- [ ] Reconcile Flux
- [ ] Verify HTTPRoute is Accepted
- [ ] Test application access via new hostname
- [ ] Monitor for errors in Envoy Gateway logs
- [ ] (Optional) Keep old Ingress temporarily for rollback
- [ ] Remove old Ingress once confident
- [ ] Update any hardcoded URLs in application configs

## Comparison: Ingress vs HTTPRoute

| Feature | Nginx Ingress | Gateway API HTTPRoute |
|---------|---------------|----------------------|
| Resource Type | `kind: Ingress` | `kind: HTTPRoute` |
| Gateway Reference | `ingressClassName: internal` | `parentRefs: envoy-internal` |
| Hostname | `spec.rules[].host` | `spec.hostnames[]` |
| Path Matching | `spec.rules[].http.paths[]` | `spec.rules[].matches[]` |
| Backend | `backend.service.name/port` | `backendRefs[].name/port` |
| TLS | `spec.tls[]` | Configured on Gateway |

## Example Migration

### Before (Nginx Ingress)

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: pinchflat
spec:
  ingressClassName: internal
  rules:
    - host: "pinchflat.${SECRET_DOMAIN}"
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: pinchflat
                port:
                  number: 80
```

### After (HTTPRoute)

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: pinchflat
spec:
  parentRefs:
    - name: envoy-internal
      namespace: network
      sectionName: https
  hostnames:
    - "pinchflat.${SECRET_DOMAIN}"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: pinchflat
          port: 80
```

## Troubleshooting

### HTTPRoute Not Accepted

```bash
kubectl describe httproute <name> -n <namespace>
```

Common issues:
- Backend service doesn't exist
- Parent Gateway not found
- Certificate issues (check Gateway status)

### Gateway Not Programmed

```bash
kubectl describe gateway -n network
```

Common issues:
- LoadBalancer IP not assigned
- Certificate secret missing
- CRDs not installed

### Application Not Responding

```bash
# Check service endpoints
kubectl get endpoints <service-name> -n <namespace>

# Check proxy logs
kubectl logs -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=<gateway-name>
```

## Future Migration Steps

1. **Phase 1 (Current):** Run both nginx and Envoy Gateway in parallel
2. **Phase 2:** Migrate 1-2 applications per day to HTTPRoutes
3. **Phase 3:** Once all apps migrated and stable for 1 week:
   - Update Envoy Gateway IPs to 10.32.8.88/87
   - Remove nginx ingress controllers
   - Clean up old Ingress resources

## References

- [Envoy Gateway Documentation](https://gateway.envoyproxy.io/)
- [Gateway API Specification](https://gateway-api.sigs.k8s.io/)
- [onedr0p's home-ops](https://github.com/onedr0p/home-ops/tree/main/kubernetes/apps/network/envoy-gateway)
