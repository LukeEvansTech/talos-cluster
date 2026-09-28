# Sentinel syslog collector

## Purpose

`sentinel-syslog` (`kubernetes/apps/security/sentinel-syslog`) is a Logstash-based syslog
collector in the `security` namespace. It listens on a LoadBalancer Service for UDP and TCP 514,
filters the stream, and ships it to Microsoft Sentinel (an Azure Log Analytics workspace) through
a Data Collection Rule, using a custom image
(`ghcr.io/lukeevanstech/logstash-sentinel`) that bundles the Microsoft Sentinel Logstash output
plugin. It is a separate collector from Wazuh (`kubernetes/apps/security/wazuh`); both can ingest
from the same on-prem syslog sources, but each ships to its own destination.

## Azure workload identity

The plugin authenticates to Azure with a federated Entra credential, not a client secret. Entra's
federated credential trusts the token subject `system:serviceaccount:security:sentinel-syslog`,
and the projected service account token (audience `api://AzureADTokenExchange`) is mounted
manually rather than through a webhook. The client ID, tenant ID, and the Data Collection
Endpoint/Rule coordinates all come from `cluster-secrets` (1Password), since none of them can live
in this public repository; they can only be populated once the corresponding `terraform-entra` and
`terraform-azure-platform` applies have created the federated credential and the Log Analytics
Data Collection Rule.

## Memory tuning

The container OOM-killed roughly every four hours until its JVM heap, Netty off-heap buffers, and
glibc's per-thread malloc arenas were all bounded; see
[KB-012](../troubleshooting/kb/012-jvm-container-rss-oom-malloc-arena-max.md) for the full
diagnosis (this app is the one that surfaced it) and
[KB-157](../troubleshooting/kb/157-digest-bump-jruby.md) for a separate incident, where a Renovate
digest bump silently moved the image's base runtime and broke the bundled plugin.

## Networking

The LoadBalancer Service is the shared LAN syslog endpoint: any source can ship logs to it, and
the pipeline distinguishes them by `[program]` and the in-message hostname rather than by source
IP. Its `externalTrafficPolicy` is `Cluster`, not `Local`, for the same reason as Wazuh's syslog
listener: under `Local`, Cilium's L2 lease-holder node can ARP the LB address without the pod
actually landing there, silently dropping external traffic
([#4070](https://github.com/LukeEvansTech/talos-cluster/pull/4070); see `docs/docs/apps/wazuh.md`
for the full incident). The `CiliumNetworkPolicy` is required because the cluster runs
default-deny ingress: external senders arrive as Cilium's `world` identity, so without the policy
the collector looks healthy while receiving nothing. It is scoped to the syslog ports only, never
the Logstash monitoring port, the same shape as Wazuh's policy.

## References

- `kubernetes/apps/security/sentinel-syslog/app/helmrelease.yaml`, the workload identity, memory
  tuning and networking settings this page documents.
- `kubernetes/apps/security/sentinel-syslog/app/networkpolicy.yaml`, the ingress policy.
- `docs/docs/apps/wazuh.md`, the full `externalTrafficPolicy` incident write-up
  ([#4070](https://github.com/LukeEvansTech/talos-cluster/pull/4070)).
