# KB-022: Container Won't Start as Non-Root (s6 / LinuxServer Image `CreateContainerConfigError`)

**Status:** Image-class gotcha. Fix is one explicit securityContext line.

## Symptom

A freshly-rendered pod for an **s6-overlay / LinuxServer-style** image (e.g. `tdarr`,
`gameyfin`) fails one of:

- `CreateContainerConfigError: container's runAsUser breaks non-root policy` (the pod never
  starts), **or**
- the container starts but dies in init with `groupmod: Permission denied` (exit 10), **or**
- `s6-applyuidgid: fatal: unable to set supplementary group list: Operation not permitted`.

A **stale** deployment of the same image may keep running, masking the problem until the next
fresh render.

## Cause

s6/LinuxServer images **must start as root** to drop privileges in their own init
(`s6-applyuidgid`, `groupmod`, …). The bjw-s **app-template** chart enforces a non-root policy
**by default**, so a pod with `runAsUser: 0` but **no explicit `runAsNonRoot: false`** renders
fine as a *stale* deployment yet is **rejected on a fresh render** by the chart's policy
(`runAsUser breaks non-root policy`).

## Fix

Set the full root securityContext **explicitly** on the app-template values:

```yaml
securityContext:
  runAsUser: 0
  runAsGroup: 0
  runAsNonRoot: false   # REQUIRED — the chart blocks runAsUser: 0 without it
```

Always set `runAsNonRoot: false` when you intentionally run root on this chart. `runAsUser: 0`
alone is not enough.

## How to recognise fast

- The error mentions **non-root policy** or an **s6 / `groupmod` privilege** failure, and the
  image is a LinuxServer/`*arr`-style s6 image.
- Related NFS-ownership note (so you don't "fix" the wrong thing): the TrueNAS `pool/*` exports
  use `mapall=…(1000)` + `all_squash`, so **every write over NFS lands as `1000:1000`
  regardless of the pod's runtime UID**. Running these images as root therefore still writes
  `1000:1000` files: "run the app as 1000 for NFS compatibility" is unnecessary here; the
  export already normalizes ownership. Hardcode `path: /mnt/pool/<dataset>` in the helmrelease
  (only `server: ${SECRET_STORAGE_SERVER}` is a variable).

## References

- bjw-s app-template (securityContext defaults): <https://bjw-s-labs.github.io/helm-charts/docs/app-template/>
- Cluster security defaults (`runAsNonRoot`, `readOnlyRootFilesystem`, `drop: ["ALL"]`) are the
  norm; this KB is the documented exception for s6 images.
