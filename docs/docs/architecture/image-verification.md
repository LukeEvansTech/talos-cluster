# Image signature verification

Every node cryptographically verifies Sidero-published container images at pull time using
[Talos `ImageVerificationConfig`](https://www.talos.dev/latest/reference/configuration/security/imageverificationconfig/)
with cosign keyless (sigstore) signatures. Live since 2026-07 (issue #2340, PR #3462); the config
lives in `talos/patches/global/machine-image-verification.yaml` and is applied to all nodes as a
global talconfig patch.

## What is verified

| Image pattern | OIDC issuer | Signing identity |
| --- | --- | --- |
| `ghcr.io/siderolabs/*` | `https://accounts.google.com` | regex: `@siderolabs.com` emails **or** `releasemgr-svc@talos-production.iam.gserviceaccount.com` |
| `factory.talos.dev/*` | `https://accounts.google.com` | `image-factory-signing@talos-production.iam.gserviceaccount.com` |

Rule semantics (Talos v1.13):

- Rules are evaluated in order; **first matching rule applies**. Matching is on registry +
  repository only, never tag or digest.
- An image matching **no** rule is pulled unverified, exactly as before this config existed. There
  are no `deny` rules, so this setup cannot block third-party images.
- The `subjectRegex` on the `ghcr.io/siderolabs/*` rule keeps the legacy personal-email identities
  as a fallback: images published before Sidero's cutover to service-account signing (including
  some tags still referenced by older Talos versions) were signed by individual
  `@siderolabs.com` engineers.

## Design decisions

- **Sidero-only scope.** Talos's verifier chokes on the newer OCI-referrers/bundle-tag signature
  format (siderolabs/talos#13639), which registries like `quay.io/cilium` use. Sidero's own images
  use the legacy `.sig`-tag scheme, which works. Third-party rules stay out until upstream settles.
- **The factory boot path is the point.** The nodes boot from
  `factory.talos.dev/installer-secureboot/<schematic>` (the schematic is pinned in
  `talos/talconfig.yaml`). Since Talos v1.14 drops `ghcr.io/siderolabs/installer` from releases
  entirely, the factory path is the sole installer route: its signing coverage is what makes this
  config worthwhile.
- **History.** A first attempt (PR #2315/#2337) was rolled back (#2339) in April 2026 because
  Sidero then signed only three images, with rotating personal-email identities, and did not sign
  the factory images at all. Issue #2340 tracked the revisit triggers; by 2026-07 all of them had
  flipped (image-factory#417, talos#13178) and the config was re-landed in #3462.

## Verifying / testing

Check a signature manually (cosign is not in the mise toolchain; run it ad hoc):

```bash
mise exec "aqua:sigstore/cosign@latest" -- cosign verify \
  --certificate-oidc-issuer=https://accounts.google.com \
  --certificate-identity=image-factory-signing@talos-production.iam.gserviceaccount.com \
  "factory.talos.dev/installer-secureboot/<schematic>:<talos-version>"
```

On-node pull-test matrix (run against any node with `talosctl image pull -n <node>`):

| Test image | Expected |
| --- | --- |
| `ghcr.io/siderolabs/installer:<current-version>` | pulled, signature verifies |
| `docker.io/library/busybox:<tag>` | pulled, no matching rule, unaffected |
| `ghcr.io/siderolabs/installer:v1.0.0` | **rejected**, pre-dates signing, proves enforcement |

The rejection looks like:

```text
image verification failed: no valid signature found: bundle tag not found
legacy signature tag not found
```

## Operational notes

- Applying the config is a **no-reboot** machine-config change (`just talos apply-node <ip>`;
  `--dry-run` first shows the document diff and confirms no reboot).
- If a Talos upgrade or image pull fails with `image verification failed`, see the
  [upgrade troubleshooting page](../operations/talos-upgrades.md#image-verification-failures):
  diagnose whether Sidero's signing identity changed before suspecting anything else.
- **Emergency bypass:** remove
  `"@./patches/global/machine-image-verification.yaml"` from the `patches` list in
  `talos/talconfig.yaml`, regenerate (`just talos gen-config`), and apply to the affected node.
  Restore it once the upstream identity question is resolved.
