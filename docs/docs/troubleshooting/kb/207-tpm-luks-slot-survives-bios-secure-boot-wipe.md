# KB-207: A Static-Passphrase LUKS Slot Insures Against a BIOS Secure Boot Key Wipe

**Status:** Reference. The dual-slot design is permanent, insuring against a real but not-yet-
triggered risk.

## Overview

Talos seals LUKS key slot 0 to PCR 7, which measures the UEFI Secure Boot state and the
PK/KEK/db/dbx certificates. A known-buggy line of BIOS firmware updates wipes those certificates
(confirmed with hardware proof in [#3552](https://github.com/LukeEvansTech/talos-cluster/pull/3552)),
which would change PCR 7 and could leave slot 0 unable to unseal. `PreserveSECBOOTKEY` only helps
on a BIOS version that already fixed the bug, not on the outgoing one that triggers it.

That specific failure has not been observed. A later, unrelated boot failure on flashed hardware
was investigated for exactly this cause and ruled it out: see
[KB-028's "Ruled out" section](028-talos-upgrade-boots-old-version-loaderentrydefault.md#ruled-out),
which found `encryptionSlot: 0` (the TPM slot) still valid after the flash, so the PCR 7 seal
survived. Slot 1 remains in place as insurance for a flash that does break the seal, not because
one has.

Talos does not self-heal a failed TPM slot: a PCR mismatch is not `ErrTokenInvalid`, so the slot is
never re-sealed automatically. Upstream declined to add a recovery-key mechanism
(siderolabs/talos#7868), which is why this repository carries its own fallback.

### Why slot 0 still binds to PCR 7

An earlier attempt dropped the PCR 7 binding entirely (`pcrs: []` on the `VolumeConfig` patch) to
remove this risk instead of insuring against it. It does not work: talhelper's serialisation drops
an empty `pcrs` list, so the rendered config carries `options: {}`, which Talos treats as unset and
defaults straight back to PCR 7. `talosctl get volumestatus` on the test node still reported
`tpmEncryptionOptions.pcrs: [7]` after applying the change. Because the change was a silent no-op
rather than a visible failure, it was reverted instead of left as dead config.

## The insurance

Every control-plane node's system and ephemeral disk encryption carries two key slots:

- **Slot 0**: TPM-sealed, bound to PCR 7. This is the normal boot path.
- **Slot 1**: a static passphrase, injected at `talhelper genconfig` time from the 1Password
  `talos-luks-fallback` item and written only to the gitignored `talos/clusterconfig/` output.
  Never commit it; this repository is public.

If slot 0 ever does fail to unseal, slot 1 keeps the node bootable while you re-seal slot 0 against
the new PCR 7 value:

1. Edit `talos/talconfig.yaml` to remove the `tpm: {}` slot 0 key from the affected node's
   `systemDiskEncryption` patch, keeping slot 1.
2. Run `just talos gen-config` to re-render `talos/clusterconfig/`. Editing `talconfig.yaml` alone
   changes nothing: `just talos apply-node` applies the already-rendered output, not the source file.
3. `just talos apply-node <node>` and let it reboot.
4. Restore the `tpm: {}` key in `talconfig.yaml`, run `just talos gen-config` again, and
   `apply-node` once more to re-seal slot 0.

**Never remove slot 1 without adding a replacement key first.** Applying a config that omits a key
slot makes Talos delete that slot on the node, not talhelper (talhelper only renders the config
file; it never touches a running node). A node with no working slot cannot be unsealed at all.

## References

- [#3552](https://github.com/LukeEvansTech/talos-cluster/pull/3552): added this fallback and
  proved the BIOS key-wipe behaviour it insures against
- [KB-028](028-talos-upgrade-boots-old-version-loaderentrydefault.md#ruled-out): confirms the flash
  happened and that the TPM seal survived it
- siderolabs/talos#7868 (no LUKS recovery-key mechanism upstream)
- `talos/talconfig.yaml`, the `systemDiskEncryption` patch on each control-plane node
