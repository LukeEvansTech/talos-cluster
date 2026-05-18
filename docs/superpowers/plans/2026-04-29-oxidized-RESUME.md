# Oxidized — Session Resume Notes (paused 2026-04-29 evening)

> **Pick this up tomorrow.** Read this file first, then continue from the "Next steps" section at the bottom.

## What's done

✅ **Spec** — `docs/superpowers/specs/2026-04-29-oxidized-design.md` — committed.
✅ **Implementation plan** — `docs/superpowers/plans/2026-04-29-oxidized.md` — committed.
✅ **Spec + plan corrections committed** — exporter port (8080 not 9001), CLI flag (`--url` not `--oxidized-url`), and metric name (`oxidized_device_status != 2 for: 48h`, replacing the non-existent `oxidized_node_last_success_timestamp_seconds`).
✅ **Branch:** `feat/observability-oxidized` in talos-cluster, three commits.
✅ **Phase 0 / Task 0.2** — exporter metric semantics resolved during planning by reading the akquinet/oxidized-exporter source. Recorded in plan.
🟡 **Phase 0 / Task 0.3** — image digests partially resolved:

```text
OXIDIZED_DIGEST=sha256:12c0155d7f7c827fd884cb9d33b8aac44fa6291a9e54499cca6e3122e90c47b9
EXPORTER_DIGEST=<unresolved — see "Open issues" below>
ALPINE_DIGEST=sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d
BUSYBOX_DIGEST=sha256:1487d0af5f52b4ba31c7e465126ee2123fe3f2305d638e7827681e7cf6c83d5e
```

Saved to `~/tmp/oxidized-resolved.env`.

## What's not done

⏸ **Phase 0 / Task 0.1** — Ruckus model. **Assumed `ruckusunleashed`** based on "ruckus r770". To be confirmed by SSHing the device or just trying it during validation deploy.
⏸ **Phase 0 / Task 0.4** — GitHub repository + deploy key. NOT created yet.
⏸ **Phase 0 / Task 0.5** — Consolidated `oxidized` 1Password item. NOT created yet.
⏸ **Phase 1, 2, 3, 4** — not started.

## Open issues / decisions needed

### Issue 1: Exporter image tag `v1.0.7` does not exist on GHCR

The readme mentions v1.0.7 as the latest release, but `ghcr.io/akquinet/oxidized-exporter` only publishes tags up through **`v1.0.5`** (plus various `sha-...` tags). The "v1.0.7 release" on GitHub may not have a corresponding container image, or the publishing pipeline failed.

**Resolution options for tomorrow:**

- **A)** Use `v1.0.5` instead. Plan/spec mention `v1.0.7` and need updating.
- **B)** Re-check the upstream — perhaps the v1.0.7 image was published with delay.
- **C)** Use one of the `sha-...` floating tags. Not ideal — Renovate won't track them.

Recommendation: **A** — switch to `v1.0.5`, fetch its digest, update spec/plan accordingly. Renovate will then auto-PR upgrades to v1.0.6+ as they land.

### Issue 2: GLiNet has no credentials in 1Password

Searched both `Home Operations` and `Talos` vaults — no `Network-GLiNet` or similar item exists. The `network-ops/.env.example` references `op://Home Operations/Network-GLiNet/...` paths, but the item itself was never created.

**Decision needed:**

- **A)** Drop GLiNet from Oxidized scope (5 devices instead of 6) — simplest.
- **B)** Create the `Network-GLiNet` 1Password item now (you'd need to populate user/pass).
- **C)** Skip until later — leave a `# TODO` placeholder in `router.db` and add when ready.

Recommendation: **A**. Adding back later is one ConfigMap edit away.

### Issue 3: Pushover application token needed

Existing `Talos/pushover` item only has `PUSHOVER_USER_KEY` and `PUSHOVER_USER_EMAIL`. Pushover requires a per-application **token** which is created by you at <https://pushover.net/apps/build> — can't be created via API.

**Action for tomorrow:**

1. Go to <https://pushover.net/apps/build>.
2. Create an app named "Oxidized" — note the application API token.
3. Decide where to store it:
    - Add a `PUSHOVER_TOKEN` field to the existing `Talos/pushover` item, OR
    - Put it directly in the new `Talos/oxidized` item we'll create.

## Discoveries from today (don't re-research these)

| Finding                                                                                                                                                                      | Location                                                                                                                       |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Cluster's ESO reads only from `Talos` 1Password vault (not `Home Operations`)                                                                                                | `kubernetes/apps/external-secrets/onepassword-connect/app/clustersecretstore.yaml` line 12                                     |
| `Network-OPNsense` already has `username` + `password` fields (so SSH access is viable, no new creds needed)                                                                 | `op item get "Network-OPNsense" --vault "Home Operations"`                                                                     |
| Ruckus item is titled `Network-Ops - Ruckus` (note spaces around the dash) — not `Network-Ruckus`                                                                            | `op item list --vault "Home Operations"`                                                                                       |
| MikroTik has SEPARATE per-device creds: `Network-MikroTik-PoE` and `Network-MikroTik-NonPoE`                                                                                 | (spec/plan currently assume shared `MIKROTIK_USERNAME/PASSWORD` — needs splitting into `MIKROTIK_POE_*` + `MIKROTIK_NONPOE_*`) |
| Exporter listens on port `8080`, flag `--url` / `-U`, metric is `oxidized_device_status` (2/1/0)                                                                             | akquinet/oxidized-exporter source (already corrected in spec/plan)                                                             |
| The user's `crane` and `skopeo` aren't in the project mise toolchain — use `docker buildx imagetools` for digests, registry API + bearer token for GHCR                      | `~/.mise.toml` and tested today                                                                                                |
| Docker daemon (OrbStack) wasn't running during today's session — `docker buildx imagetools inspect` worked anyway because it goes registry-direct, but `docker pull` did not | OrbStack — start it before running digest commands tomorrow                                                                    |

## Plan adjustments needed in `2026-04-29-oxidized.md` before proceeding

Spotted today but not yet edited into the plan:

1. **Remove `MIKROTIK_USERNAME`/`MIKROTIK_PASSWORD`, replace with `MIKROTIK_POE_USERNAME`/`MIKROTIK_POE_PASSWORD` + `MIKROTIK_NONPOE_USERNAME`/`MIKROTIK_NONPOE_PASSWORD`** — affects Task 1.5 (router.db template), Task 1.6 (ExternalSecret), Task 1.7 (init container env vars).
2. **Update exporter image tag** if Issue 1 resolves to v1.0.5 — affects Task 1.7.
3. **Drop GLiNet line from `router.db` template** if Issue 2 resolves to drop scope — affects Task 1.5.
4. **The `oxidized` 1Password item must live in `Talos` vault, not `Home Operations`** — affects Task 0.5 (one-line clarification).

## Next steps when resuming

In order:

1. **Decide on the three open issues** (exporter tag, GLiNet scope, Pushover app token) — small block of decisions, ~5 min.
2. **Edit the plan** to reflect those decisions + the four spec adjustments above. One commit.
3. **Finish Phase 0:**
    - Get the v1.0.5 (or whichever) exporter digest.
    - Create Pushover app + record token.
    - Create GitHub repository + deploy key (Task 0.4).
    - Create `Talos/oxidized` 1Password item using `op item create` with cross-vault `op read` for device creds (Task 0.5).
4. **Dispatch first Phase 1 subagent** for Task 1.1 (directory + namespace registration). From then on, subagent-driven flow runs as planned.

## Quick start commands for tomorrow

```bash
# 1. Make sure Docker is running (for re-fetching digests if needed)
open -a OrbStack   # or whichever Docker provider

# 2. Get back to where we were
cd ~/GIT/LukeEvansTech/talos-cluster
git checkout feat/observability-oxidized
git log --oneline -5

# 3. Re-read this file, then the plan, then start chipping away at "Next steps"
cat docs/superpowers/plans/2026-04-29-oxidized-RESUME.md
```
