# KB-028: Talos Upgrade Installs Successfully but the Node Boots the Old Version (NVRAM Wipe → `LoaderEntryDefault`)

**Status:** Fix proven on two nodes (v1.13.6 → v1.13.7). Trigger identified as the BIOS flash off the buggy line, not a Talos regression. Expect a recurrence on the next upgrade until a Talos boot entry is restored to `BootOrder`.

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

**A BIOS flash wiped the NVRAM, including the EFI boot entries Talos relies on.** The upgrade itself is fine; only the boot *selection* is broken.

Normally Talos writes the new UKI to the ESP and creates a direct EFI boot entry for it, which the firmware then boots. After an NVRAM wipe that entry is gone and is never re-established in `BootOrder`, so the firmware falls through to the removable-media fallback `\EFI\BOOT\BOOTX64.efi` — systemd-boot — which picks its entry from the `LoaderEntryDefault` EFI variable. That variable named the outgoing version, so the node booted the old UKI.

The installer log states it plainly. Note the empty list: Talos cannot find any boot entry it created on a previous upgrade either.

```text
Current BootOrder: [2]
Existing boot entries: [2]
Found existing Talos Linux UKI boot entries: []
created Talos Linux UKI boot entry at index 0
```

`Boot0002` is a firmware-generated fallback entry. The entry Talos creates on each upgrade does not survive into `BootOrder`, so systemd-boot decides every boot.

### Why it started when it did

[#3552](https://github.com/LukeEvansTech/talos-cluster/pull/3552) recorded all three sleds on **BIOS 1.4** on 2026-07-15, and documented with hardware proof that flashing *from* the buggy line (≤ 1.7) wipes the Secure Boot keys despite `PreserveSECBOOTKEY: true` — because the preservation logic runs in the *outgoing* BIOS. The boards now report **BIOS 1.9**, so that flash happened, and it took the boot entries along with the keys.

The timeline is the giveaway, and it is worth trusting over any theory about the Talos release:

| Date       | Event                                         | Outcome    |
| ---------- | --------------------------------------------- | ---------- |
| 2026-07-11 | Talos v1.13.5 → v1.13.6                       | Success    |
| 2026-07-15 | #3552 lands; sleds recorded on BIOS 1.4       | —          |
| after that | BIOS flashed to 1.9 (out of band, not in Git) | —          |
| 2026-07-26 | Talos v1.13.6 → v1.13.7                       | **Failed** |

This was the first Talos upgrade after the flash.

### Ruled out

Two plausible-sounding explanations the evidence does **not** support. Both were tested.

- **TPM unseal failing and falling back to the slot-1 static key.** `talosctl get volumestatus STATE` and `EPHEMERAL` report `encryptionSlot: 0` (the TPM slot) on all three nodes, so PCR 7 is intact and the sealed policy is valid. The slot-1 fallback added by #3552 is not engaged. It remains the right insurance for a future flash; it is simply not what happened here.
- **A Talos v1.13.7 installer regression pointing `LoaderEntryDefault` at the outgoing version.** Contradicted twice: one node's variable names the version it *installed*, not the outgoing one, and on another the variable reappeared after a plain reboot with no installer running at all. Something writes it at boot; it is not the installer setting it backwards.

## Confirming it on a node

Talos persists service logs to `/var/log`, and they **survive the reboot**, so the failed boot's install output is still readable. This is the single most useful command here:

```bash
export TALOSCONFIG=./talos/clusterconfig/talosconfig
talosctl -n <node-ip> read /var/log/machined.log.1 | grep -Ei 'upgrade|install|UKI|BootOrder'
```

A successful install ends like this — which is the whole point, the install is not the problem:

```text
copying /usr/install/amd64/vmlinuz.efi to /boot/EFI/EFI/Linux/Talos-v1.13.7.efi
updating EFI variables
Current BootOrder: [2]
Found existing Talos Linux UKI boot entries: []
installation of v1.13.7 complete
[talos] upgrade completed successfully: exit_code=0
```

Then confirm which entry actually booted, and what steered it:

```bash
talosctl -n <node-ip> get bootedentry -o yaml          # -> bootedEntry: talos-v1.13.6.efi

L=4a67b082-0a4c-41cf-b6c7-440b29bb8c4f
for v in LoaderEntryDefault LoaderEntries; do
  echo -n "$v = "
  talosctl -n <node-ip> read /sys/firmware/efi/efivars/$v-$L |
    python3 -c "import sys;print(', '.join(x for x in sys.stdin.buffer.read()[4:].decode('utf-16-le').split(chr(0)) if x))"
done
```

If `LoaderEntries` contains the new UKI while `LoaderEntryDefault` names the old one, this KB applies. Confirm the trigger with `talosctl -n <node-ip> read /sys/class/dmi/id/bios_version`.

## Fix

The install is already on disk — **do not re-run the upgrade**. Delete `LoaderEntryDefault` and reboot; with no default set, systemd-boot selects the newest UKI, which is the new version.

The host mounts `efivarfs` read-only, so this cannot be done with `talosctl` alone:

```text
none /sys/firmware/efi/efivars efivarfs ro,nosuid,nodev,noexec,relatime 0 0
```

A privileged pod can mount its **own** `efivarfs` instance read-write instead. Use `nodeName` (not `nodeSelector`) so it bypasses the scheduler and still lands on a cordoned node:

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

Clearing the immutable flag is required: efivarfs marks variables immutable, so `unlink` fails without it.

Then drain and reboot the node normally:

```bash
kubectl cordon <node>
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force --timeout=7m
talosctl -n <node-ip> reboot
# ~6 minutes of POST on this hardware, then:
talosctl -n <node-ip> get bootedentry -o yaml   # -> talos-v1.13.7.efi
kubectl uncordon <node>
```

Wait for `HEALTH_OK` before starting the next node — see [KB-019](019-cordon-control-plane-breaks-ceph-mon-quorum.md), a cordoned control-plane node parks its affinity-pinned mon and OSDs in `Pending` and holds Ceph in `HEALTH_WARN` for as long as the cordon stands.

Finally clear the failed CR so tuppr re-plans against reality:

```bash
kubectl delete talosupgrade talos
flux reconcile ks tuppr-upgrades -n system-upgrade --with-source=false
```

### A node may self-correct, and can mislead you

If the install happens to leave only new-version UKIs on the ESP, the stale default matches nothing, systemd-boot falls back to newest, and that node boots the new version unaided. One of the three did exactly this. Do not conclude from one healthy node that the fleet is fine — check `bootedentry` on every node.

### Do not fix it by repointing the default

Setting `LoaderEntryDefault` to the new version — for example by pressing `d` in the systemd-boot menu over IPMI — boots the node correctly today but leaves a default set, which recreates this trap on the next upgrade. **Deleting** it is what restores correct behaviour.

## Open items

1. **The real fix is to get a Talos UKI boot entry back into `BootOrder`** so the firmware stops falling through to systemd-boot. Until then every upgrade needs the workaround. Worth doing from the BIOS boot menu in the next maintenance window.
2. Because the workaround is applied after the fact, a tuppr-driven upgrade will always fail its first node and stop the batch. Budget for driving the rest by hand.
3. Identify what writes `LoaderEntryDefault` at boot. It reappeared on one node after deletion and a plain reboot, so deleting it is not necessarily permanent.

## References

- KB-004 — [Talos Patch Rollout Gotchas (TUPPR)](004-talos-patch-rollout-gotchas-tuppr.md), the other reason an upgrade leaves a node on the old version
- KB-019 — [Cordoning a Control-Plane Node Breaks Ceph Mon Quorum](019-cordon-control-plane-breaks-ceph-mon-quorum.md)
- #3552 — the LUKS2 slot-1 fallback key, which documents the BIOS key-wipe behaviour that triggers this
- systemd-boot variables: <https://systemd.io/BOOT_LOADER_INTERFACE/>
