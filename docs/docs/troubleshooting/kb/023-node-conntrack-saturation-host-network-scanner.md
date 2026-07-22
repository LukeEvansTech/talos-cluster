# KB-023: Node Conntrack Table Saturates from a Host-Network Scanner

**Status:** Resolved for scanopy (scoped to the LAN NIC + single Deployment); the **rule
generalizes to any host-network sweeper**.

## Symptom

`NodeHighNumberConntrackEntriesUsed` (warning) fires on **every node at once**, conntrack tables
sit at ~100% (e.g. `262144/262144`), and new connections start dropping **cluster-wide**. The
trigger is deploying a network scanner / sweeper.

## Cause

A scanner running with **`hostNetwork: true`** in a poll/daemon mode **auto-scans every subnet
its host has an interface on**. On a Talos/Cilium node that includes the cilium virtual
interfaces, and therefore the **pod CIDR `10.42.0.0/16` + service CIDR `10.43.0.0/16`**
(~131k IPs). At the default scan rate it port-scans the **entire internal cluster network**; run
as a **DaemonSet** across all control-plane nodes, it saturates **every** node's conntrack
table.

A full conntrack table drops new connections cluster-wide: this is a **whole-cluster hazard**,
not just a scanner problem. (Stopping the scanner drains conntrack back to <2% within ~3 min,
which confirms it is the sole cause.)

## Fix

- **Scope the scanner to the physical LAN NIC** so it never sees the Cilium CIDRs (the big
  lever):

  ```sh
  SCANOPY_INTERFACES=<lan-nic>   # --interfaces; default = ALL interfaces
  ```

  Verify the NIC name is **identical on all nodes** before hardcoding it in a workload (different
  hardware can enumerate interfaces differently).
- Prefer a **single-replica Deployment over a DaemonSet** on a flat LAN. One scanner covers the
  whole `/24`; a per-node DaemonSet triples the load scanning the same subnet. (scanopy: use
  `strategy: Recreate` + a stable `SCANOPY_NAME`.)
- **Cap concurrency** (`SCANOPY_CONCURRENT_SCANS`, default auto ~10-20) and set a conservative
  per-discovery Speed profile. Newer scanopy moved per-scan rate (`scan-rate-pps`, port batch)
  to the **UI per-discovery "Speed" tab**; daemon-level rate flags are ignored.
- **Optional headroom:** raise `net.netfilter.nf_conntrack_max` via Talos `machine.sysctls`.

**General rule:** any hostNetwork scanner/sweeper on this cluster **must** be scoped to the
physical NIC / real LAN. Never let it discover the Cilium pod/service CIDRs.

## How to recognise fast

- The conntrack alert appears **immediately after deploying/enabling a scanner**, on **all
  nodes** simultaneously. Emergency relief: `flux suspend hr <scanner>` and park the workload
  (`nodeSelector` → no nodes) to drain conntrack, then re-deploy scoped.
- **VLAN caveat:** `SCANOPY_INTERFACES` only scopes **L2 / local-interface** scanning. The
  scanner still **L3-scans remote routable subnets** it discovers via SNMP routing tables, so as
  new VLANs come online and become routable, conntrack load can climb again. Narrow scope
  per-discovery in the UI and keep `nf_conntrack_max` headroom. Re-check conntrack after a VLAN
  buildout.

## References

- Conntrack tuning: <https://www.kernel.org/doc/Documentation/networking/nf_conntrack-sysctl.txt>
- Related: [KB-008](008-cilium-cross-node-pod-networking-breaks.md) (conntrack also surfaces as a
  Cilium cross-node diagnostic).
