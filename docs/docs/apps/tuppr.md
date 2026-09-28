# tuppr

## Purpose

`kubernetes/apps/system-upgrade/tuppr` drives Talos and Kubernetes version rolls through
`TalosUpgrade` and `KubernetesUpgrade` custom resources. See
[upgrade playbooks](../operations/upgrade-playbooks.md#tuppr) for the known breaking patterns and
the per-release checklist.

## Drain-first mitigation for the shutdown-unmount stall

Draining each node before its reboot lets the node flush and unmount its Ceph (`ceph-block`)
volumes while the mons on the other nodes are still alive. Without this, a shutdown-time unmount
stall can wedge a node half-down and require an IPMI reset (GH #2728). Combined with the default
`parallelism: 1` (one node at a time) and the `CephCluster` `HEALTH_OK` health check, this is the
"drain-first, one-node-at-a-time" mitigation that issue identified.

## Alert silence design during a Talos upgrade

A rolling Talos upgrade reboots each node in turn, and the alertnames silenced below are the
expected consequence of a node being deliberately absent, not something worth paging on.
Alertmanager's root route sends every severity to Pushover, so without these silences each run
would page several times per node.

tuppr's matchers are static, with no way to bind a silence to the node currently rebooting, so
every name below is silenced cluster-wide for the run. A second node failing while the first is
down is masked under these names, deliberately survivable, because the alerts that say quorum is
actually going are not silenced anywhere here: `etcdInsufficientMembers`, `etcdMembersDown`,
`etcdNoLeader`, `CephMonDownQuorumAtRisk`, `MiroirVolumeQuorumLost`. `CephMonDown` is silenced and
`CephMonDownQuorumAtRisk` is not; that pairing is the whole design, so any new name added here
must stay on the "expected consequence" side of it. `TargetDown` is the widest of the silenced
names and has no such backstop: it covers every scrape target, not just the rebooting node's.

The two silence groups:

- Ceph: `CephMonDown`, `CephOSDDown`, `CephOSDHostDown`, `CephHealthWarning`, `CephPGsUnclean`,
  `CephPGsInactive`, `CephFilesystemDegraded`. One node down takes its OSDs and a mon with it,
  degrading PGs into `HEALTH_WARN` until the cluster re-converges. `CephHealthError` and the
  full/near-full alerts are deliberately not silenced; those are not expected consequences of a
  reboot.
- Node/kubelet absence and the scrape failures that follow from it: `KubeNodeUnreachable`,
  `KubeNodeNotReady`, `KubeNodeReadinessFlapping`, `KubeletDown`, `TargetDown`.

`maxDuration: 1h` is the safety catch: a run still holding a silence past it starts alerting
again, so a wedged upgrade re-pages rather than staying quiet. One hour comfortably covers one
node (drain, reboot, Ceph re-convergence) at `parallelism: 1`, and the budget re-arms once the
hold releases. Every alertname here was checked against the rules actually loaded in the cluster;
a silence naming an alert that does not exist is a no-op that looks like it is working.
