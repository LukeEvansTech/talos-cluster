# miroir

## Purpose

`miroir` provisions `miroir-local` node-local volumes from loopfile-backed pools. `app` runs the
controller and agents; `config` defines the node group, StorageClass and snapshot classes.

## CSIStorageCapacity and the overcommit incident

`storageCapacity.enabled: true` makes the external-provisioner publish a `CSIStorageCapacity`
object per node and storage class from the driver's `GetCapacity` RPC, so the scheduler can steer a
`WaitForFirstConsumer` pod away from a node whose pool is already full. Before this was enabled, the
scheduler had no such signal and placed pods however it liked, leaving `CreateVolume` to refuse
afterward.

Over time, nearly every VolSync cache volume accreted onto a single node: 194 of 210 volumes,
against 10 and 6 on the other two. [#5098](https://github.com/LukeEvansTech/talos-cluster/pull/5098)
then raised the kopia cache to 16Gi, correctly, since the shared repository index was 7.57Gi and
growing about 25MiB a day. 194 of those resizes succeeded on the busiest node and consumed the
remaining headroom:

|              | provisioned | ceiling (capacity x2) | headroom |
| ------------ | ----------- | --------------------- | -------- |
| busiest node | 3570 GiB    | 3572 GiB              | ~3 GiB   |
| peer         | 417 GiB     | 3572 GiB              | 3155 GiB |
| peer         | 353 GiB     | 3572 GiB              | 3219 GiB |

The last two resizes and every new volume on that node were refused with a `ResourceExhausted`
error citing the capacity x2 overcommit guard, even though real disk on that node was only 45%
used. The `--overcommit-ratio=2` and `--free-space-ratio=20` provisioner guards were not changed;
publishing `CSIStorageCapacity` gives the scheduler information it was missing rather than raising
the limit ([#5110](https://github.com/LukeEvansTech/talos-cluster/pull/5110)).

This fix stops the imbalance from getting worse; it does not undo it. The 194 existing caches stay
where they are until an operator deletes their PVCs by hand. VolSync then recreates each one
automatically on its next sync, on a node with room, at the cost of one cold run.

Agents republish pool stats every 60s (`agent.poolStatsInterval`). Until the first publish after a
rollout, the scheduler treats an unpublished (node, class) pair as unfit, so new `miroir-local`
provisioning waits about a minute; already-bound volumes are unaffected.
