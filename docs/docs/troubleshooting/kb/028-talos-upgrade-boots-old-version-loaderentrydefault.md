# KB-028: Talos Upgrade Installs Successfully but the Node Boots the Old Version (NVRAM Wipe → `LoaderEntryDefault`)

**Status:** Fix proven on five nodes across two upgrades (v1.13.6 → v1.13.7, then v1.13.7 → v1.13.8, where all three needed it). The trigger is the BIOS flash off the buggy line, not a Talos regression. `BootOrder` turned out **not** to be the lever — see the section below — so recurrence depends entirely on whether `LoaderEntryDefault` comes back.

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

On the v1.13.7 → v1.13.8 run, **none** of the three self-corrected. Plan for the workaround on every node until the `LoaderEntryDefault`-writer in open item 3 is identified.

### The fix needs two boot cycles, not one

Deleting `LoaderEntryDefault` and rebooting is not enough on its own. On all three nodes the first boot after the deletion **still came up on the old version**, then the node rebooted itself unprompted a few minutes later and came up on the new one.

This matters because the obvious check — reading `bootedentry` as soon as the node is back — reports the old version and looks exactly like the fix having failed. It has not. Wait for the second boot before drawing any conclusion:

```bash
# Wait for the kubelet to report the target version rather than eyeballing the first boot
until kubectl get node <node> -o jsonpath='{.status.nodeInfo.osImage}' | grep -q 'v1.13.8'; do sleep 20; done
talosctl -n <node-ip> get bootedentry -o yaml   # only meaningful once the above returns
```

Budget roughly 15 minutes per node — two POST cycles on this hardware — rather than one.

### Do not fix it by repointing the default

Setting `LoaderEntryDefault` to the new version — for example by pressing `d` in the systemd-boot menu over IPMI — boots the node correctly today but leaves a default set, which recreates this trap on the next upgrade. **Deleting** it is what restores correct behaviour.

## `BootOrder` is not the lever it looks like

The original open item 1 read "the real fix is to get a Talos UKI boot entry back into `BootOrder` so the firmware stops falling through to systemd-boot." That was based on a false premise, and the 2026-08-09 run disproved it. Recording the evidence so nobody spends a maintenance window on it.

The entry was never missing. Every node has had it all along — it simply was not referenced by `BootOrder`:

```text
BootCurrent: 0002
BootOrder: 0002
Boot0000* Talos Linux UKI  HD(1,GPT,…)/\EFI\boot\BOOTX64.efi
Boot0002* UEFI OS          HD(1,GPT,…)/\EFI\BOOT\BOOTX64.EFI
```

**Both entries point at the same file.** `Boot0000` is named "Talos Linux UKI" but its device path is the removable-media fallback `\EFI\boot\BOOTX64.efi` — systemd-boot — not a versioned UKI under `\EFI\Linux\`. Promoting it therefore changes nothing: the firmware hands control to systemd-boot either way, and systemd-boot picks the entry, so `LoaderEntryDefault` decides the boot no matter what `BootOrder` says.

`BootOrder` was set to `0000,0002` on all three nodes anyway, since that matches what the installer intends when it logs `created Talos Linux UKI boot entry at index 0`, and it costs nothing. **It is not a fix**, and the node still booted via `Boot0002` afterwards. Do not treat it as one.

This can be done in-cluster — no BIOS or IPMI needed. Same privileged-pod trick as the deletion, with `efibootmgr` instead of hand-written bytes:

```yaml
# containers[0]: image alpine:3.22, securityContext.privileged: true
command:
  - /bin/sh
  - -c
  - |
    set -e
    apk add --no-cache efibootmgr
    mount -t efivarfs none /sys/firmware/efi/efivars
    efibootmgr                    # inspect
    efibootmgr -o 0000,0002       # keep 0002 second as the working fallback
    umount /sys/firmware/efi/efivars
```

Because both entries resolve to the same loader, keeping `0002` in the list means the worst case is the behaviour you already had.

## Open items

1. **Identify what writes `LoaderEntryDefault`.** This is the whole ballgame — it is the only variable that actually steers the boot here. After the v1.13.8 upgrade the variable is **absent on all three nodes**, which is the good state: with no default set, systemd-boot selects the newest UKI, so the next upgrade should boot correctly unaided. Read it before the next upgrade — if it has reappeared naming v1.13.8, the trap is armed again and the writer needs to be found.
2. Because the workaround is applied after the fact, a tuppr-driven upgrade will always fail its first node and stop the batch. Budget for driving the rest by hand: fix the node, clear the CR, let tuppr re-plan, repeat. Three nodes took about an hour.

## References

- KB-004 — [Talos Patch Rollout Gotchas (TUPPR)](004-talos-patch-rollout-gotchas-tuppr.md), the other reason an upgrade leaves a node on the old version
- KB-019 — [Cordoning a Control-Plane Node Breaks Ceph Mon Quorum](019-cordon-control-plane-breaks-ceph-mon-quorum.md)
- #3552 — the LUKS2 slot-1 fallback key, which documents the BIOS key-wipe behaviour that triggers this
- systemd-boot variables: <https://systemd.io/BOOT_LOADER_INTERFACE/>
