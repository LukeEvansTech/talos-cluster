# KB-025: CephFS "Module ceph not found" on Talos Is a Built-in, Not a Missing Module

**Status:** Misdiagnosis corrected 2026-07-09. CephFS support **is present** in the Talos kernel;
the June 2026 "Talos ships no `ceph` kernel module" conclusion (and the comments it left in the
rook-ceph HelmReleases) was wrong. Re-enablement is planned — see the
[adoption roadmap](../../operations/jory-homeops-adoption.md).

## Symptom

Attempting to use a CephFS volume (or manually probing for support) fails with:

```text
modprobe: FATAL: Module ceph not found in directory /lib/modules/<kernel>
```

There is no `ceph` module anywhere under `/lib/modules/<kernel>/kernel/fs/`, and the Talos
extension catalog offers no ceph module extension. Every signal appears to say "this kernel
cannot mount CephFS", so CephFS gets written off as unsupported on Talos.

That is exactly what happened here during the June 2026 llmkube migration: CephFS was enabled,
mounts failed, the modprobe error was taken as proof of a missing kernel module, and the
filesystem, StorageClass, and cephfs CSI driver were all reverted. The (wrong) conclusion was
baked into comments in `kubernetes/apps/rook-ceph/rook-ceph/cluster/helmrelease.yaml` and
`csi-drivers/helmrelease.yaml`, and the model-storage layer was redesigned around RWO
workarounds (per-model PVCs plus staging Jobs) as a consequence.

## Cause

**The CephFS client is compiled directly into the Talos kernel (`CONFIG_CEPH_FS=y`), not built
as a loadable module.** `modprobe` only searches for loadable `.ko` files, so it reports
"Module ceph not found" for any built-in — the error is expected and harmless. A built-in
filesystem needs no modprobe at all; `mount -t ceph` works directly.

Verified on all three nodes (Talos v1.13.5, kernel 6.18.36):

```console
$ talosctl -n <node> read /lib/modules/6.18.36-talos/modules.builtin | grep ceph
kernel/fs/ceph/ceph.ko
kernel/net/ceph/libceph.ko

$ talosctl -n <node> read /proc/filesystems | grep ceph
nodev   ceph
```

The upstream kernel config (`siderolabs/pkgs`, `release-1.13`, `kernel/build/config-amd64`)
confirms it: `CONFIG_CEPH_FS=y` and `CONFIG_CEPH_FS_POSIX_ACL=y`. Note the adjacent trap for
anyone grepping that 7,800-line file: `CONFIG_CEPH_LIB=y` (the messenger library used by RBD)
appears first and is easy to mistake for the only ceph option present — an automated review
pass made exactly that error and "confirmed" the misdiagnosis.

Why the June mounts actually failed was never pinned down (candidates: the ceph-csi mounter's
modprobe handling at the time, or a CSI configuration detail such as the msgr2-only setting —
this cluster sets `requireMsgr2: true`, which kernel CephFS clients need the `ms_mode` mount
option to negotiate). The kernel itself was never the blocker.

## Correct diagnosis checklist

Before concluding a filesystem is unsupported on Talos (or any immutable OS):

1. `talosctl read /proc/filesystems` — a registered filesystem type means the kernel supports
   it **right now**, regardless of what modprobe says.
2. `talosctl read /lib/modules/<kernel>/modules.builtin` — built-ins live here, not under
   `kernel/fs/`.
3. Only if both are empty is the "missing module" theory alive — then check the upstream
   kernel config for `=m` vs `=y` vs absent, reading carefully (`CEPH_LIB` ≠ `CEPH_FS`).
4. A modprobe failure alone proves nothing about built-ins.

## Lessons

- `modprobe: FATAL: Module X not found` has two readings: "not present" and "present but
  built-in". `/proc/filesystems` and `modules.builtin` disambiguate in seconds.
- When a diagnosis drives a redesign (here: RWO per-model PVCs, staging Jobs with
  `kustomize.toolkit.fluxcd.io/reconcile: disabled`, a disabled llmkube model cache), record
  the *evidence* alongside the conclusion so it can be re-checked cheaply later.
- Comparable clusters are a signal: joryirving/home-ops kernel-mounts CephFS on the identical
  Talos v1.13.5 with zero schematic or extension changes — if a peer does the "impossible",
  re-test the premise.
