# Scrypted

Camera and doorbell bridge in the `home` namespace. Brings Ring devices into
Apple Home with HomeKit Secure Video (HKSV), and is the intended home for the
Reolink cameras in a later phase.

## Purpose

- Node/TypeScript camera hub (`ghcr.io/koush/scrypted`) chosen over Frigate or a
  Home Assistant–only setup because it ships first-party **Ring** *and*
  **Reolink** plugins plus the strongest HKSV pipeline.
- **Phase 0 (current):** the official Ring plugin bridges the existing Ring
  fleet (2× Doorbell Pro 2, Doorbell 2nd Gen, Stick Up Cam Battery, Floodlight
  Cam Wired Plus, Spotlight Cam Plus Battery, Indoor Cam) into HomeKit. Traffic
  is cloud-relayed through the Ring account — expected at this stage.
- **Phase 1+:** Reolink cameras (Doorbell PoE/WiFi, Duo/Elite Floodlight, Argus
  4 Pro, E1 Pro) via `@scrypted/reolink`. Nothing here blocks local RTSP/ONVIF.
- Internal-only: `scrypted.${SECRET_DOMAIN}` on `envoy-internal`.

## Division of labour with Homebridge

Scrypted is **additive** — Homebridge stays.

- **Scrypted** — cameras and doorbells only (HKSV, low-latency streams).
- **Homebridge** — everything else, and optionally Ring Alarm/sensors/modes via
  `homebridge-ring`. That plugin has no HKSV by design, so cameras must never
  live there. If `homebridge-ring` is ever enabled, disable its camera support
  to avoid duplicate accessories in Apple Home.

!!! warning "Homebridge is currently an empty, unpaired bridge"
    At the time Scrypted was added, Homebridge had `pairedClients: {}` (never
    paired to HomeKit), an empty `cachedAccessories`, only the `homebridge-dummy`
    plugin, and a crash-looping Avahi (`Failed to create runtime directory
    /run/avahi-daemon/`). It is therefore **not** a working reference for
    HomeKit networking — see the Avahi fix tracked separately. Scrypted does not
    depend on it.

## Design decisions

- **Networking — Multus macvlan on the untagged legacy LAN (`iot-legacy` NAD).**
  HomeKit pairing (HAP) needs L2 adjacency with the HomeKit hubs, or an mDNS
  reflector. An mDNS probe run on both segments settled it empirically: both
  Apple TVs answered `_airplay._tcp` / `_companion-link._tcp` / `_sleep-proxy._udp`
  on the **untagged legacy LAN**, while **VLAN 68 returned zero services** — so
  no reflection exists into the IoT VLAN. Scrypted therefore attaches to
  `iot-legacy` (like `matter-server`), *not* the `iot` NAD used by Home
  Assistant, zigbee2mqtt and mosquitto. Upstream's own compose uses
  `network_mode: host` for exactly this reason; macvlan is the cluster's
  equivalent of real LAN presence.
- **Static IP + MAC.** The NAD uses static IPAM, so the pod carries a fixed
  address and a MAC continuing the `02:00:00:00:00:0X` series used by the other
  Multus workloads. The chosen address was verified unused before assignment —
  two neighbouring candidates answered ping. It must sit outside the OPNsense
  DHCP pool.
- **`strategy: Recreate`.** The pod owns a fixed L2 address and a ReadWriteOnce
  PVC; a RollingUpdate would briefly run two pods sharing one MAC/IP and
  deadlock on the volume.
- **UI routed over plain HTTP (`11080`), not HTTPS (`10443`).** Scrypted serves
  the *same* application on both ports (`SCRYPTED_INSECURE_PORT` /
  `SCRYPTED_SECURE_PORT`). Routing the gateway at `11080` avoids introducing a
  `BackendTLSPolicy` + skip-verify for Scrypted's self-signed cert — the
  repository has no such pattern anywhere today. The HTTPS port stays available on the pod
  for direct access. TLS to the browser is still terminated by the gateway.
- **Avahi deliberately left off.** `SCRYPTED_DOCKER_AVAHI` is not set: with a
  real LAN interface, Scrypted's built-in mDNS advertiser reaches the hubs
  directly, and enabling the bundled Avahi would reproduce Homebridge's
  crash-loop.
- **Runs as root.** The official image's entrypoint manages the plugin runtime
  and npm-installs into `/server/volume`. Same documented exception as
  Homebridge; most apps here run as UID 1000.
- **Image variant `-noble-full`.** This is the variant upstream publishes as
  `:latest` (identical digest). Renovate's docker versioning keeps updates
  within the same suffix family. The `-noble-nvidia` variant is the drop-in
  swap if GPU transcoding is ever wanted — the cluster's NVIDIA L4s and the
  `runtimeClassName: nvidia` pattern are available, but Phase 0 needs no GPU.

## Storage

- `/server/volume` → a 10Gi `ceph-block` PVC with the standard volsync
  treatment. This holds plugin installs, the device database and **the HomeKit
  pairing keys** — losing it unpairs every accessory in Apple Home.
- **No NVR volume.** HKSV clips live in iCloud, so Phase 0 provisions no bulk
  storage. A commented TrueNAS NFS mount and the `SCRYPTED_NVR_VOLUME=/nvr`
  variable are left in the HelmRelease for when the Reolink NVR plugin is
  adopted.

## Secrets

**Nothing secret belongs in Git — this repository is public.** Scrypted has no
`ExternalSecret`. Ring account credentials and the 2FA code are entered
**interactively in the Scrypted UI** on first configuration, and Scrypted stores
the resulting refresh token inside `/server/volume` (which is why that PVC is
backed up rather than reproducible from Git).

The admin account for the management UI is likewise created on first launch.

## First-run checklist

1. Reach the UI at `scrypted.${SECRET_DOMAIN}` and create the admin
   account.
2. Install the **Ring** plugin; sign in with the Ring account and complete 2FA.
   Confirm all devices appear under Devices.
3. Install the **HomeKit** plugin and pair from the Home app.
4. Pair the **doorbells as standalone accessories** rather than bridged —
   per Scrypted's docs this is markedly more reliable for doorbell press
   notifications. Cameras can stay bridged.
5. Enable **HKSV recording** on at least one camera and verify clips play back
   in the Home app.
6. Check no duplicate camera accessories appeared in Apple Home.

## Gotchas

- **HAP port clash.** Scrypted's HomeKit plugin uses its own configurable port,
  independent of Homebridge's. The two advertise as separate bridges and are on
  different IPs, so co-scheduling on one node is fine.
- **Pairing state is precious.** Restoring the PVC from a volsync snapshot
  restores pairing; deleting it silently forces a re-pair of everything.
- **The IoT VLAN is not an option for HomeKit** until an mDNS reflector exists
  between VLAN 68 and the LAN the Apple TVs sit on.
