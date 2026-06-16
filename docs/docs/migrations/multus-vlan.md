# Multus VLAN Migration Guide

!!! note "Completed migration"
    This page is a record of a migration that is already complete. The steps below are preserved as history; some current-state paths, names, and commands have since drifted. Commands and paths flagged in review are corrected inline — see the Architecture and Operations sections for the present-day setup.

This guide documents the process of migrating Home Assistant from the management network (<mgmt-net>/24) to a dedicated IoT VLAN for network isolation.

## Current State

- **Primary Network**: Cilium (10.42.0.0/16 pods, 10.43.0.0/16 services)
- **Management Network**: <mgmt-net>/24
- **Home Assistant Multus IP**: <ha-mgmt-ip>/24
- **Physical Interface**: enp1s0np0
- **VLAN Configuration**: None (direct macvlan on physical interface)

## Target State

- **Primary Network**: Cilium (unchanged)
- **Management Network**: <mgmt-net>/24 (unchanged)
- **IoT VLAN**: VLAN 70 - <iot-vlan-net>/24 (new)
- **Home Assistant Multus IP**: <ha-iot-ip>/24
- **Physical Interface**: enp1s0np0.70 (VLAN tagged)

## Prerequisites

### Network Switch Configuration

1. **Create VLAN 70** on your managed switch
2. **Configure trunk port** to Kubernetes nodes:

    ```text
    # Example for most switches:
    - Set port mode to "Trunk" or "Tagged"
    - Allow VLANs: 1 (untagged/native), 70 (tagged)
    ```

3. **Configure DHCP/Gateway** for IoT VLAN:
    - Gateway: <iot-gateway>
    - DHCP range: <iot-dhcp-start>-<iot-dhcp-end> (optional)
    - DNS: Your DNS server

4. **Configure Firewall Rules**:

    ```text
    IoT VLAN (<iot-vlan-net>/24) Rules:
    - ALLOW: IoT → Internet (for firmware updates)
    - ALLOW: Management Network (<mgmt-net>/24) → IoT (for Home Assistant access)
    - DENY: IoT → Management Network (isolate IoT devices)
    - ALLOW: IoT → IoT (devices can communicate with each other)
    ```

### Verify Switch Configuration

Before proceeding, verify VLAN configuration:

```bash
# From a device on the IoT VLAN
ping <iot-gateway>  # Should reach gateway

# From management network (if firewall allows)
ping <iot-gateway>  # Should reach gateway
```

## Migration Steps

### Step 1: Update Talos Configuration

Add VLAN interface configuration to all nodes.

**File**: `talos/talconfig.yaml`

```yaml
nodes:
    - hostname: "<node1>"
      ipAddress: "<node1-ip>"
      # ... existing configuration ...
      networkInterfaces:
          - deviceSelector:
                hardwareAddr: "<node1-mac>"
            dhcp: false
            addresses:
                - "<node1-ip>/24"
            routes:
                - network: "0.0.0.0/0"
                  gateway: "<mgmt-gateway>"
            mtu: 1500
            vip:
                ip: "<vip>"
            # ADD THIS SECTION:
            vlans:
                - vlanId: 70
                  dhcp: false
                  mtu: 1500
                  # No IP address needed - used only for container networking
```

**Repeat for all nodes** (<node2>, <node3>) with their respective MAC addresses:

- <node2>: `<node2-mac>`
- <node3>: (add the MAC address)

### Step 2: Generate and Apply Talos Configuration

```bash
# Generate new Talos configs
talhelper genconfig

# Apply to each node (one at a time to avoid downtime)
talosctl apply-config -n <node1-ip> -f talos/clusterconfig/kubernetes-<node1>.yaml
talosctl apply-config -n <node2-ip> -f talos/clusterconfig/kubernetes-<node2>.yaml
talosctl apply-config -n <node3-ip> -f talos/clusterconfig/kubernetes-<node3>.yaml

# Verify VLAN interface exists on each node
talosctl -n <node1-ip> get links | grep "enp1s0np0.70"
talosctl -n <node2-ip> get links | grep "enp1s0np0.70"
talosctl -n <node3-ip> get links | grep "enp1s0np0.70"
```

**Expected output**: You should see the VLAN interface listed with VLAN ID 70.

### Step 3: Update Cilium Device Configuration (Optional)

If you want Cilium to also be aware of the VLAN interface (recommended for better routing):

**File**: `kubernetes/apps/kube-system/cilium/app/helm/values.yaml`

```yaml
# Change from:
devices: enp+

# To:
devices: enp+,enp+.70
```

Then reconcile:

```bash
flux reconcile kustomization cilium -n kube-system
```

### Step 4: Update NetworkAttachmentDefinition

This is the key change that switches from management network to IoT VLAN.

**File**: `kubernetes/apps/kube-system/multus/networks/iot.yaml`

```yaml
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
    name: iot
    namespace: kube-system
spec:
    config: |-
        {
          "cniVersion": "0.3.1",
          "name": "iot",
          "plugins": [
            {
              "type": "macvlan",
              "master": "enp1s0np0.70",
              "mode": "bridge",
              "ipam": {
                "type": "static"
              }
            }
          ]
        }
```

**Key changes**:

- `master`: `enp1s0np0` → `enp1s0np0.70` (adds VLAN tagging)

### Step 5: Update Home Assistant IP Configuration

**File**: `kubernetes/apps/home/homeassistant/app/helmrelease.yaml`

Find the Multus annotation section and update the IP:

```yaml
defaultPodOptions:
    annotations:
        k8s.v1.cni.cncf.io/networks: |-
            [{
              "name": "iot",
              "namespace": "kube-system",
              "ips": ["<ha-iot-ip>/24"],
              "mac": "<mac>"
            }]
```

**Key changes**:

- `ips`: `["<ha-mgmt-ip>/24"]` → `["<ha-iot-ip>/24"]`

### Step 6: Apply Changes

Commit all changes and let Flux reconcile:

```bash
# Stage all changes
git add talos/talconfig.yaml \
        kubernetes/apps/kube-system/multus/networks/iot.yaml \
        kubernetes/apps/home/homeassistant/app/helmrelease.yaml

# Commit with descriptive message
git commit -m "feat(network): migrate Home Assistant to IoT VLAN 70

- Add VLAN 70 interface to all Talos nodes
- Update Multus NetworkAttachmentDefinition to use VLAN interface
- Change Home Assistant IP from <ha-mgmt-ip> to <ha-iot-ip>
- Enables network isolation for IoT devices"

# Push to trigger Flux reconciliation
git push

# Force reconcile for faster deployment
flux reconcile source git flux-system -n flux-system
flux reconcile kustomization multus-networks -n kube-system
flux reconcile kustomization homeassistant -n home
```

### Step 7: Verify Migration

```bash
# 1. Check NetworkAttachmentDefinition is updated
kubectl get network-attachment-definitions -n kube-system iot -o yaml

# 2. Wait for Home Assistant pod to restart (Flux will recreate it)
kubectl get pods -n home -l app.kubernetes.io/name=homeassistant -w

# 3. Verify new network configuration
HA_POD=$(kubectl get pod -n home -l app.kubernetes.io/name=homeassistant -o jsonpath='{.items[0].metadata.name}')

# Check IP addresses
kubectl exec -n home $HA_POD -- ip addr show

# Expected output:
# eth0: 10.42.0.x/32 (Cilium)
# net1: <ha-iot-ip>/24 (Multus IoT VLAN) ← Should be new IP

# 4. Test connectivity to IoT VLAN gateway
kubectl exec -n home $HA_POD -- ping -c 3 <iot-gateway>

# 5. Test Internet connectivity (via Cilium)
kubectl exec -n home $HA_POD -- ping -c 3 8.8.8.8

# 6. Test Kubernetes service discovery (via Cilium)
kubectl exec -n home $HA_POD -- nslookup kubernetes.default.svc.cluster.local
```

### Step 8: Update DNS Records (if needed)

If you have DNS records pointing to the old IP:

```bash
# Update any static DNS entries from:
homeassistant.${SECRET_INTERNAL_DOMAIN}  A  <ha-mgmt-ip>

# To:
homeassistant.${SECRET_INTERNAL_DOMAIN}  A  <ha-iot-ip>
```

## Validation Tests

### Test 1: Dual Network Interfaces

```bash
kubectl exec -n home $HA_POD -- ip route show

# Expected output should show:
# - Default route via Cilium (eth0)
# - Direct route to <iot-vlan-net>/24 via net1
```

### Test 2: Home Assistant Web UI

Access Home Assistant at:

- Via HTTPRoute: <https://homeassistant.example.com>
- Direct IP (from device on IoT VLAN): <http://<ha-iot-ip>:8123>

### Test 3: Device Discovery

From Home Assistant UI:

1. Navigate to **Settings** → **Devices & Services**
2. Click **Add Integration**
3. Try to discover devices (Chromecast, HomeKit, etc.)
4. Devices on IoT VLAN should be discovered

### Test 4: MQTT Communication (if using Mosquitto)

```bash
# If you deployed Mosquitto MQTT broker
kubectl exec -n home $HA_POD -- \
  mosquitto_pub -h mosquitto.home.svc.cluster.local -t test -m "hello"
```

## Troubleshooting

### Issue 1: Pod Stuck in Pending

**Symptom**: Pod doesn't start after migration

**Check**:

```bash
kubectl describe pod -n home $HA_POD
```

**Common causes**:

- VLAN interface not created on Talos node
- NetworkAttachmentDefinition not found
- IP address conflict

**Solution**:

```bash
# Verify VLAN interface exists on the node where pod is scheduled
NODE=$(kubectl get pod -n home $HA_POD -o jsonpath='{.spec.nodeName}')
talosctl -n $NODE get links | grep enp1s0np0.70

# If missing, reapply Talos config
talosctl apply-config -n $NODE -f talos/clusterconfig/kubernetes-$NODE.yaml
```

### Issue 2: No Network Connectivity on net1

**Symptom**: Can't ping gateway from pod

**Check**:

```bash
kubectl exec -n home $HA_POD -- ip addr show net1
kubectl exec -n home $HA_POD -- ip route show
```

**Common causes**:

- Switch VLAN not configured properly
- Firewall blocking traffic
- Wrong gateway IP

**Solution**:

```bash
# Test from Talos node itself
talosctl -n <node1-ip> get addresses | grep 192.168.70

# If node can't reach VLAN, check switch configuration
```

### Issue 3: Device Discovery Not Working

**Symptom**: Home Assistant can't discover devices on IoT VLAN

**Common causes**:

- mDNS reflector needed on router/gateway
- Firewall blocking multicast traffic
- Devices on different VLAN

**Solution**:

1. Enable mDNS reflector on your router (if devices are on different subnets)
2. Ensure multicast is allowed on IoT VLAN
3. Check firewall rules allow Home Assistant → IoT devices

### Issue 4: Can't Access Home Assistant Externally

**Symptom**: HTTPRoute / Gateway API not working after migration

**Note**: HTTPRoute / Gateway API still uses Cilium (eth0), not Multus (net1). This should continue working.

**Check**:

```bash
kubectl get svc -n home homeassistant
kubectl get httproute -n home homeassistant
```

**Solution**: HTTPRoute / Gateway API routing is independent of Multus networking and should not be affected.

## Rollback Procedure

If migration causes issues, you can rollback quickly:

### Quick Rollback (Revert NetworkAttachmentDefinition Only)

```bash
# Revert iot.yaml to use management network
cat <<EOF | kubectl apply -f -
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: iot
  namespace: kube-system
spec:
  config: |-
    {
      "cniVersion": "0.3.1",
      "name": "iot",
      "plugins": [
        {
          "type": "macvlan",
          "master": "enp1s0np0",
          "mode": "bridge",
          "ipam": {
            "type": "static"
          }
        }
      ]
    }
EOF

# Update Home Assistant IP back to management network
kubectl edit helmrelease -n home homeassistant
# Change ips: ["<ha-iot-ip>/24"] → ["<ha-mgmt-ip>/24"]

# Delete pod to force recreation
kubectl delete pod -n home -l app.kubernetes.io/name=homeassistant
```

### Full Rollback (Including Talos)

```bash
# Revert git changes
git revert HEAD
git push

# Flux will automatically reconcile back to previous state
flux reconcile kustomization multus-networks -n kube-system
flux reconcile kustomization homeassistant -n home

# Remove VLAN interfaces from Talos (optional - they won't hurt if left)
# Edit talconfig.yaml to remove vlans section, then:
talhelper genconfig
talosctl apply-config -n <node1-ip> -f talos/clusterconfig/kubernetes-<node1>.yaml
talosctl apply-config -n <node2-ip> -f talos/clusterconfig/kubernetes-<node2>.yaml
talosctl apply-config -n <node3-ip> -f talos/clusterconfig/kubernetes-<node3>.yaml
```

## Post-Migration Checklist

- [ ] VLAN 70 created on switch
- [ ] Trunk ports configured for Kubernetes nodes
- [ ] Firewall rules configured for IoT isolation
- [ ] Talos VLAN interfaces created on all nodes
- [ ] NetworkAttachmentDefinition updated to use VLAN interface
- [ ] Home Assistant IP updated to IoT VLAN range
- [ ] Changes committed to Git
- [ ] Home Assistant pod restarted successfully
- [ ] Dual interfaces verified (Cilium + Multus VLAN)
- [ ] Connectivity tests passed (gateway, internet, K8s services)
- [ ] Device discovery working in Home Assistant
- [ ] HTTPRoute/external access working
- [ ] DNS records updated (if needed)
- [ ] Documentation updated

## Additional VLAN Networks

To add more VLANs (e.g., VLAN 80 for Cameras, VLAN 90 for VPN):

1. **Add VLAN to Talos** (`talconfig.yaml`):

    ```yaml
    vlans:
        - vlanId: 70 # IoT
          dhcp: false
          mtu: 1500
        - vlanId: 80 # Cameras (new)
          dhcp: false
          mtu: 1500
    ```

2. **Create NetworkAttachmentDefinition**:

    ```yaml
    # kubernetes/apps/kube-system/multus/networks/cameras.yaml
    apiVersion: k8s.cni.cncf.io/v1
    kind: NetworkAttachmentDefinition
    metadata:
        name: cameras
        namespace: kube-system
    spec:
        config: |-
            {
              "cniVersion": "0.3.1",
              "name": "cameras",
              "plugins": [
                {
                  "type": "macvlan",
                  "master": "enp1s0np0.80",
                  "mode": "bridge",
                  "ipam": {
                    "type": "static"
                  }
                }
              ]
            }
    ```

3. **Use in pod annotations**:

    ```yaml
    annotations:
        k8s.v1.cni.cncf.io/networks: |-
            [{
              "name": "iot",
              "namespace": "kube-system",
              "ips": ["<ha-iot-ip>/24"]
            }, {
              "name": "cameras",
              "namespace": "kube-system",
              "ips": ["<cam-vlan-ip>/24"]
            }]
    ```

## References

- [Multus CNI Documentation](https://github.com/k8snetworkplumbingwg/multus-cni)
- [Talos Network Configuration](https://www.talos.dev/latest/reference/configuration/#networkconfig)
- [Cilium Multi-Network Support](https://docs.cilium.io/en/stable/network/concepts/multi-networking/)
- [onedr0p/home-ops Reference](https://github.com/onedr0p/home-ops)

## Support

If you encounter issues not covered in this guide:

1. Check pod events: `kubectl describe pod -n home $HA_POD`
2. Check Multus logs: `kubectl logs -n kube-system -l app.kubernetes.io/name=multus`
3. Check Talos network config: `talosctl -n <node> get addresses`
4. Review this guide's troubleshooting section

---

**Migration Guide Version**: 1.0
**Last Updated**: 2025-10-29
**Cluster**: talos-cluster
