# KB-028: Talos Upgrade Installs Successfully but the Node Boots the Old Version (`LoaderEntryDefault`)

**Status:** Workaround documented and proven (v1.13.6 → v1.13.7). Root cause is a firmware/installer interaction; not fixed upstream. **Expect this on every future Talos upgrade** until it is.

## Symptom

A tuppr-driven Talos upgrade fails with a version mismatch, even though nothing looks broken:

```text
ERROR Failed to verify node  node=<node>
  error="node <node> version mismatch: current=v1.13.6, target=v1.13.7"
INFO  Node upgrade failed     node=<node>
```

The `TalosUpgrade` CR lands in `Failed` with `lastError: "Job failed permanently"` and `retries: 0`, and stops the batch so the remaining nodes are never attempted.

What makes this confusing is that every individual step *succeeded*:

- The drain completed.
- The installer image was pulled and signature-verified.
- The install reported `exit_code=0`.
- The node rebooted, came back `Ready`, and was **uncordoned automatically**.
- `kubectl get nodes` shows it healthy — just still on the **old** `osImage`.

This is not the KB-004 failure mode. There the reboot never happened; here the reboot happens and the node boots the *wrong* image.

## Cause

Three things combine. Only the third is specific to this cluster's hardware.

1. **The installer writes the new UKI and creates a direct EFI boot entry.** From the node's own log (see below), the v1.13.7 installer copies `Talos-v1.13.7.efi` onto the ESP and creates a `Boot0000` entry pointing at it.

2. **The installer also sets the systemd-boot `LoaderEntryDefault` EFI variable to the version that is currently running** — not to the version it just installed. After a v1.13.6 → v1.13.7 upgrade the variable reads `Talos-v1.13.6.efi`. On a machine that boots the entry from step 1 this is invisible and harmless.

3. **This hardware's firmware discards the boot entry Talos creates.** `BootOrder` stays pinned at the pre-existing fallback entry on every node, and the installer log confirms Talos cannot find any boot entry it created on a previous upgrade either:

   ```text
   Current BootOrder: [2]
   Existing boot entries: [2]
   Found existing Talos Linux UKI boot entries: []
   created Talos Linux UKI boot entry at index 0
   ```

So the firmware ignores `Boot0000`, falls through to `\EFI\BOOT\BOOTX64.efi` (systemd-boot), and systemd-boot obediently honours `LoaderEntryDefault` — booting the **old** UKI. tuppr then compares the running version against the target, sees a mismatch, and correctly reports failure.

Earlier upgrades worked because the variable was absent, and systemd-boot with no default selects the newest entry. The upgrade *to* v1.13.7 runs the v1.13.7 installer, which is what starts setting it.

### Confirming the diagnosis

Talos persists service logs to `/var/log`, and they **survive the reboot**, so the failed boot's install output is still readable. This is the single most useful command here:

```bash
export TALOSCONFIG=./talos/clusterconfig/talosconfig
talosctl -n <node-ip> read /var/log/machined.log.1 | grep -Ei 'upgrade|install|UKI|BootOrder'
```

A successful install looks like this — note it ends in success, which is the whole point:

```text
sd-boot: found existing UKIs during upgrade: [Talos-v1.13.5.efi Talos-v1.13.6.efi]
removing old UKI: /boot/EFI/EFI/Linux/Talos-v1.13.5.efi
copying /usr/install/amd64/vmlinuz.efi to /boot/EFI/EFI/Linux/Talos-v1.13.7.efi
updating EFI variables
Current BootOrder: [2]
Found existing Talos Linux UKI boot entries: []
created Talos Linux UKI boot entry at index 0
installation of v1.13.7 complete
[talos] upgrade completed successfully: exit_code=0
```

Then confirm which entry actually booted and why:

```bash
talosctl -n <node-ip> get bootedentry -o yaml     # -> bootedEntry: talos-v1.13.6.efi
```

The EFI variables tell the rest of the story. `LoaderEntries` proves the new UKI is present and bootable; `LoaderEntryDefault` is what overrides it:

```bash
L=4a67b082-0a4c-41cf-b6c7-440b29bb8c4f
talosctl -n <node-ip> read /sys/firmware/efi/efivars/LoaderEntryDefault-$L |
  python3 -c "import sys;print(sys.stdin.buffer.read()[4:].decode('utf-16-le').strip(chr(0)))"
```

If that prints the **old** version while `LoaderEntries` lists the new one, this KB applies.

## Fix

The install is already on disk — **do not re-run the upgrade**. Delete the `LoaderEntryDefault` variable and reboot; systemd-boot then falls back to selecting the newest UKI, which is the new version.

The host mounts `efivarfs` read-only, so this cannot be done with `talosctl` alone:

```text
none /sys/firmware/efi/efivars efivarfs ro,nosuid,nodev,noexec,relatime 0 0
```

A privileged pod can mount its **own** `efivarfs` instance read-write instead. Set `nodeName` (not `nodeSelector`) so it bypasses the scheduler and still lands on a cordoned node:

```yaml
---
apiVersion: v1
kind: Pod
metadata:
  name: efivar-fix
  namespace: default
spec:
  nodeName: <node> # must be nodeName: the target is usually cordoned
  restartPolicy: Never
  tolerations:
    - operator: Exists
  containers:
    - name: fix
      image: python:3.13-alpine
      securityContext:
        privileged: true
      command:
        - /bin/sh
        - -c
        - |
          set -e
          mkdir -p /ev && mount -t efivarfs none /ev
          python3 - <<'PY'
          import fcntl, os, struct
          P = "/ev/LoaderEntryDefault-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
          FS_IOC_GETFLAGS, FS_IOC_SETFLAGS, FS_IMMUTABLE_FL = 0x80086601, 0x40086602, 0x10
          assert os.path.exists(P), "variable not present - nothing to do"
          with open(P, "rb") as f:
              print("current value:", f.read()[4:].decode("utf-16-le").strip("\x00"))
          fd = os.open(P, os.O_RDONLY)
          flags = struct.unpack("l", fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack("l", 0)))[0]
          fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("l", flags & ~FS_IMMUTABLE_FL))
          os.close(fd)
          os.unlink(P)
          print("deleted:", not os.path.exists(P))
          PY
          umount /ev
```

The `chattr -i` step is required: efivarfs marks variables immutable, so `unlink` fails without clearing the flag first.

Then drain and reboot the node normally. It comes back on the new version:

```bash
kubectl cordon <node>
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force --timeout=7m
talosctl -n <node-ip> reboot
# ~6 minutes of POST on this hardware, then:
talosctl -n <node-ip> get bootedentry -o yaml   # -> talos-v1.13.7.efi
kubectl uncordon <node>
```

Wait for `HEALTH_OK` before moving to the next node — see [KB-019](019-cordon-control-plane-breaks-ceph-mon-quorum.md), a cordoned control-plane node parks its affinity-pinned mon and OSDs in `Pending` and holds Ceph in `HEALTH_WARN` for as long as the cordon stands.

Finally, clear the failed CR so tuppr re-plans against reality:

```bash
kubectl delete talosupgrade talos
flux reconcile ks tuppr-upgrades -n system-upgrade --with-source=false
```

### A node may self-correct

If the install happens to evict the old UKI from the ESP (Talos keeps a limited number and removes the oldest), the stale `LoaderEntryDefault` points at a file that no longer exists, systemd-boot finds no match, and it selects the newest entry — so that node boots the new version unaided. One of the three nodes behaved this way. Do not assume from one healthy node that the fleet is fine; check each one.

### Do not fix it by repointing the default

Setting `LoaderEntryDefault` to the *new* version (for example by pressing `d` in the systemd-boot menu over IPMI) boots the node correctly today but leaves the variable set, which recreates exactly this trap on the next upgrade. **Deleting** it restores the state a healthy node is in.

## Open items

1. The installer arguably should not point `LoaderEntryDefault` at the outgoing version, or should clear it once the new entry is in place. Worth raising with upstream.
2. The firmware discarding Talos's `BootXXXX` entry is the underlying enabler and affects all nodes here. Until that changes, every Talos upgrade needs the workaround above.
3. Because the workaround is post-hoc, a tuppr-driven upgrade will always report `Failed` for the first node and stop the batch. Budget for driving the remaining nodes by hand, or clear the variable on every node before starting.

## References

- KB-004 — [Talos Patch Rollout Gotchas (TUPPR)](004-talos-patch-rollout-gotchas-tuppr.md), the other reason an upgrade leaves a node on the old version
- KB-019 — [Cordoning a Control-Plane Node Breaks Ceph Mon Quorum](019-cordon-control-plane-breaks-ceph-mon-quorum.md)
- systemd-boot variables: <https://systemd.io/BOOT_LOADER_INTERFACE/>
- tuppr: <https://github.com/home-operations/tuppr>
