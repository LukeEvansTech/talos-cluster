# etcd defragmentation CronJob

Checks etcd fragmentation on every control-plane member daily and defragments all of them, one at a
time, once any member passes the threshold in `app/configmap.yaml`.

## How it authenticates

A `talos.dev/v1alpha1` `ServiceAccount` (`app/talosserviceaccount.yaml`) asks Talos to mint a
talosconfig into the Secret `etcd-defrag-talosconfig`, which Talos keeps renewed. Talos only honours
that request in the namespaces named by `machine.features.kubernetesTalosAPIAccess`
(`talos/patches/controller/machine-features.yaml`), which is why this app lives in `system-upgrade`
and not `kube-system`. The role is `os:admin` because etcd defragmentation needs it and
`os:operator` is not in `allowedRoles`.

Until November 2026 the job used a copy of the operator's client certificate held in 1Password.
That certificate expired, and the script reported 0% fragmentation for every failed query, so the
job stayed green for five days while doing nothing. Two things changed as a result: the credential
is now minted and rotated by Talos, and any failed or unreadable query fails the Job.

## Where `talosctl` comes from

The signed `ghcr.io/siderolabs/talosctl` image is mounted as an image volume at `/talos`. Nothing is
downloaded when the job runs, and Renovate tracks the image like any other.

## Members are discovered, not listed

The script asks etcd for its members through the node the pod runs on and takes each member's
client address from the answer, so no node address is kept in this repository.

## Running it by hand

```bash
kubectl -n system-upgrade create job --from=cronjob/etcd-defrag etcd-defrag-manual
kubectl -n system-upgrade logs job/etcd-defrag-manual -f
kubectl -n system-upgrade delete job etcd-defrag-manual
```

A failed Job is the signal that something is wrong; `KubeJobFailed` picks it up.
