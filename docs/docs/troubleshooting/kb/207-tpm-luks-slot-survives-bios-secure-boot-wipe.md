# KB-207: A Static-Passphrase LUKS Slot Survives a BIOS Secure Boot Key Wipe

**Status:** Reference. The dual-slot design is permanent; the risk it guards against is accepted,
not fixed, because Talos and the affected BIOS vendor have no fix planned.

## Symptom

A control-plane node fails to boot after a BIOS firmware update. Talos cannot unseal the system or
ephemeral disk, because the TPM-sealed LUKS key slot no longer unlocks.

## Cause

Talos seals LUKS key slot 0 to PCR 7, which measures the UEFI Secure Boot state and the
PK/KEK/db/dbx certificates. A known-buggy line of BIOS firmware updates wipes those certificates,
so PCR 7 changes on the next boot and slot 0 can no longer unseal. `PreserveSECBOOTKEY` only helps
on a BIOS version that already fixed the bug, not on the outgoing one that triggers it. This was
confirmed on one control-plane node's firmware update, and because every control-plane node runs
the same firmware line, a fleet-wide update carries the same risk to etcd quorum all at once.

Talos does not self-heal a failed TPM slot: a PCR mismatch is not `ErrTokenInvalid`, so the slot is
never re-sealed automatically. Upstream declined to add a recovery-key mechanism
(siderolabs/talos#7868).

### Why slot 0 still binds to PCR 7

An earlier attempt dropped the PCR 7 binding entirely (`pcrs: []` on the `VolumeConfig` patch) to
remove this failure mode instead of working around it. It does not work: talhelper's serialisation
drops an empty `pcrs` list, so the rendered config carries `options: {}`, which Talos treats as
unset and defaults straight back to PCR 7. `talosctl get volumestatus` on the test node still
reported `tpmEncryptionOptions.pcrs: [7]` after applying the change. Because the change was a
silent no-op rather than a visible failure, it was reverted instead of left as dead config.

## Fix

Every control-plane node's system and ephemeral disk encryption carries two key slots:

- **Slot 0**: TPM-sealed, bound to PCR 7. This is the normal boot path.
- **Slot 1**: a static passphrase, injected at `talhelper genconfig` time from the 1Password
  `talos-luks-fallback` item and written only to the gitignored `talos/clusterconfig/` output.
  Never commit it; this repository is public.

When a BIOS update wipes the Secure Boot keys and slot 0 stops unsealing, slot 1 keeps the node
bootable. Recovery is manual: remove the TPM key, apply and reboot, then re-add the TPM key and
apply and reboot again to re-seal slot 0 against the new PCR 7 value.

**Never remove slot 1 without adding a replacement first.** talhelper's `syncKeys()` deletes any
key slot missing from the rendered config, and a node with no working slot cannot be unsealed at
all.

## References

- siderolabs/talos#7868 (no LUKS recovery-key mechanism upstream)
- `talos/talconfig.yaml`, the `systemDiskEncryption` patch on each control-plane node
