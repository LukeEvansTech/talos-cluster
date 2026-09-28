# VictoriaLogs

## Purpose

`kubernetes/apps/observability/victoria-logs` runs the single-node VictoriaLogs server (`app/`)
and its log collector DaemonSet (`collector/`), which ships Kubernetes pod logs into it.

## Native syslog listener

The server's syslog listener is a local sink for senders that don't fit the sentinel-syslog
pipeline. Some hosts ship logs directly here to close a compliance finding that flagged them with
no remote log target at all, and stay on this local pipeline by decision rather than being routed
through the shared relay. Any other sender that wants a full local copy alongside that relay can
use the same listener.

- It listens on both TCP and UDP, but senders should prefer TCP. Syslog over UDP truncates at 480
  bytes, and both current senders can lose the tail of a longer message that way.
- `syslog.timezone: "UTC"` is set explicitly because RFC3164 timestamps carry no timezone field,
  and both current senders already keep their clocks in UTC.
- The listener binds `:5514`, not `:514`, because the pod runs non-root. The LoadBalancer Service
  in `extraObjects` remaps the LAN-facing `514` to the pod's `5514`.

## Tailnet access for the seedbox collector

`extraObjects` adds a second, dedicated `ClusterIP` Service (`victoria-logs-tailnet`) alongside
the chart's own server Service. The chart's Service is headless (`clusterIP: None`, the
StatefulSet default), and the tailscale-operator refuses to publish a headless Service
("headless Services are not supported"). `clusterIP` is immutable, so the existing Service can't
be converted in place; this second Service exists purely to give the operator something it will
publish. It's exposed under the tailnet hostname `victoria-logs`, which the seedbox's Vector
collector uses to push its Docker and journald logs over Tailscale (#3385).

## Collector volume mount propagation

The collector DaemonSet overrides the chart's `defaultVolumeMounts` to add
`mountPropagation: HostToContainer` on the `/var/lib` mount (#3729). The chart's default _private_
propagation snapshots the host's mount tree at pod start: any CSI staging mount that happens to be
live at that moment stays pinned inside the collector's mount namespace forever, since kubelet's
later `NodeUnstageVolume` cannot propagate into a private mount. The RBD image stays mapped and
the PVC can never attach on another node, stalling with `rbd image ... is still being used`.
`HostToContainer` lets those unmounts reach the collector's mount namespace instead.

## Networking

`victoria-logs-syslog`'s LoadBalancer uses `externalTrafficPolicy: Cluster`, not `Local`, for the
same reason `wazuh-syslog` does (see [wazuh's docs](wazuh.md#networking)): Cilium's L2 lease
election can hand the address to a node without the serving pod, which then drops the traffic
under `Local`. Days of lost logs on sentinel-syslog first surfaced this
([#4070](https://github.com/LukeEvansTech/talos-cluster/pull/4070)).

The syslog Service's `targetPort`s (`syslog-tcp`, `syslog-udp`) name the chart's own container
ports instead of a hardcoded number. Setting the `syslog.listenAddr` args on the server makes the
chart declare its ports under those same names, so a future listener port change needs no matching
edit on the Service.
