# KB-008: Cross-Node Pod Networking Breaks (Cilium)

**Status:** Two distinct causes, each with a different fix. Identify the cause before acting —
the fixes do not overlap.

## Symptom

One node's **pod-to-pod** traffic to pods on the other nodes is intermittently or
"flippingly" lost (cross-node CoreDNS lookups succeed only 0–50% of the time), while that
node's **host / underlay** traffic (node-IP pings) is perfect. Paths that don't involve the
affected node (peer ↔ peer) are fine.

Reliable independent canary: the affected node's **spegel** pod goes `0/1` (P2P bootstrap DNS
i/o timeouts). Cilium's drop monitor shows nothing for the lost traffic, and NIC counters are
clean (`rx_crc_errors=0`).

## Cause

Tell the two variants apart by **route stability** and **recent changes**:

| | Variant A — stale peer state | Variant B — Talos version regression |
|---|---|---|
| Trigger | A node was **down/cordoned for hours** (e.g. a failed upgrade), then rejoined | A recent **Talos minor/patch bump** |
| Routes | Peer pod-CIDR routes are wrong but **stable** | Peer pod-CIDR routes **flap** (count oscillates 0↔1↔2) |
| Where the bad state lives | On the **long-running peer** nodes, about the churned node | On the upgraded node itself, **load-triggered** |
| agent log | — | spams `"Fallback node addresses updated" ... device=*` ~2–3/min |

### Variant A — stale Cilium datapath state on the peers

After a node sits cordoned for an extended period and its endpoints churn, the long-running
peer nodes hold **stale cross-node datapath state** about it. Cilium BPF maps (including
conntrack) **survive agent restarts**, so the affected node's own reboots, `cilium-agent`
restarts, a full DaemonSet rollout, and even `cilium bpf ct flush` on all nodes do **not**
clear it — the bad state isn't on the affected node.

### Variant B — a Talos release regression flapping the routes

A specific Talos release (historically v1.13.3) regressed cross-node routing: the peer
pod-CIDR direct routes flap in and out, correlated with workload scheduling (the flap returns
the instant a workload reschedules onto the node, so it's load-triggered, not stale state).
Routes go stable while the node is drained/quiet and break again under load. `cilium bpf ct flush`,
agent restart, plain reboot, and cordon+drain+reboot all fail to hold.

## Fix

### Variant A — reboot the **other** (peer) nodes, one at a time

A node reboot wipes its BPF maps (tmpfs) and re-registers, clearing the stale per-node state.
Reboot the long-running peers (graceful `talosctl reboot`, **not** powercycle), one at a time.
After the peers cycle, the affected node's cross-node DNS returns to stable and its spegel pod
recovers `1/1`. Do **not** waste time on a version downgrade, single-node Cilium restart, or
CT flush — none of them clear it.

### Variant B — roll the Talos version back

Revert the version pins in `talos/talenv.yaml`, `talos/talconfig.yaml`, and
`kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml` to the last good release,
push, and let TUPPR downgrade the fleet. Recovery is immediate on the good version. Renovate
won't auto-merge installer bumps, so it will re-open a PR back to the bad version — **don't
merge it**; wait for the next release and test cross-node DNS on the previously-affected node
before trusting it.

## How to recognise fast

- Symptom signature above (cross-node pod traffic broken/flipping, host traffic clean,
  spegel `0/1`).
- **Check the route stability and recent Talos version first.** Flapping routes + a recent
  version bump → Variant B (roll back). Stable-but-wrong routes after a long node outage →
  Variant A (reboot the peers).
- Diagnostic gotchas: `one.one.one.one` resolves poorly via this cluster's CoreDNS upstream —
  use `github.com` as the external probe. `cilium-dbg service list | grep <svcIP>` drops the
  indented backend lines, making a 2-backend service look like 1.

## References

- Cilium troubleshooting: <https://docs.cilium.io/en/stable/operations/troubleshooting/>
- Related: [KB-004](004-talos-patch-rollout-gotchas-tuppr.md) (the failed-upgrade cordon that
  often precedes Variant A).
