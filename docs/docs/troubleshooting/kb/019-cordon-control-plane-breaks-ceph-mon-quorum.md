# KB-019: Cordoning a Control-Plane Node Breaks Ceph Mon Quorum

**Status:** Known footgun. The fix (uncordon) is instant; the lesson is *don't cordon to
steer a pod* on this cluster.

## Symptom

Minutes after `kubectl cordon <node>` on a control-plane node — often done to "steer a single
unrelated pod elsewhere" — a burst of **critical** Ceph alerts fires:

- `CephMonDown`
- `CephMonDownQuorumAtRisk`
- `CephOSDDownHigh`

The affected mon/osd pods show `FailedScheduling`, while the workload you were actually trying
to move may not even have rescheduled yet.

## Cause

rook-ceph runs **one mon per control-plane node**, and each mon/osd pod is pinned to its node
by **node affinity** (mon-a → node A, mon-b → node B, mon-c → node C). `cordon` adds a
`NoSchedule` taint. If that node's mon (or an OSD) restarts or is rescheduled **while the node
is cordoned**, it cannot return to its required node → `FailedScheduling`. With only three mons,
losing one mon's home node drops the cluster below a 2/3 quorum, hence the *critical* quorum
alert.

The same trap applies to a node left cordoned by a **failed Talos upgrade** (see
[KB-004](004-talos-patch-rollout-gotchas-tuppr.md)) — the Ceph risk persists for as long as the
node stays cordoned.

## Fix

```sh
kubectl uncordon <node>
```

Quorum recovers within **~60s**: the mon reschedules onto its home node, all init/main
containers reach `Started` in ~25s, quorum is restored, all OSDs come back up, and Ceph returns
to `HEALTH_OK`.

**Don't cordon a control-plane node to move a pod here** — the displaced Ceph daemons matter
more than the pod you're relocating. To move a *non-Ceph* pod off a node:

- `kubectl delete pod` it and let the scheduler re-pick, **or**
- apply a temporary node-affinity / `nodeSelector` tweak to the workload.

If you genuinely must cordon (real maintenance), expect the Ceph alerts and uncordon as soon as
possible.

## How to recognise fast

- The alert storm starts **right after a `cordon`**, and a `kubectl get pod -n rook-ceph -o wide`
  shows a mon/osd `Pending`/`FailedScheduling` whose required node is the one you just tainted.
- A control-plane node that "can't start containers" is usually a **red herring** — the nodes
  start containers fine. Earlier "node can't run pods" suspicions traced to helm-remediation
  thrash killing pods mid-create, not a node defect (see
  [KB-015](015-slow-image-pulls-exceed-helmrelease-timeout.md)).

## References

- Rook-Ceph mon health: <https://rook.io/docs/rook/latest/Storage-Configuration/Advanced/ceph-mon-health/>
- Related: [KB-004](004-talos-patch-rollout-gotchas-tuppr.md) (a failed upgrade leaves a node
  cordoned), [KB-008](008-cilium-cross-node-pod-networking-breaks.md) (the other "long-cordoned
  node" hazard).
