# smtp2graph delivery monitoring

`smtp2graph` is the cluster's SMTP-to-Microsoft-Graph relay, in the `infrastructure` namespace
(`kubernetes/apps/infrastructure/smtp2graph`). This page covers why its monitoring measures
delivery outcome rather than reachability, and the sidecar and canary design behind that.

## Why delivery, not reachability

Between 2026-08-10 and 2026-08-19 the relay accepted mail and then failed every Graph send with
`ErrorSendAsDenied`: 353 messages lost, 2,824 errors, zero delivered. The SMTP listener stayed
healthy the whole time, so a liveness probe, a TCP check or a Gatus probe would all have stayed
green. Nothing noticed for nine days. Root cause was an Exchange RBAC-for-Applications scope
issue, fixed outside this repository; the metrics sidecar and `prometheusrule.yaml` (PR #4402)
exist so the next occurrence is caught in minutes.

smtp2graph v1.1.5 exposes no metrics of its own: it listens on `:25` and nothing else, and its
config schema has no metrics or health options. The sidecar in `helmrelease.yaml`
(`exporter/exporter.py`) is the only source of delivery telemetry, built from two signals:

- **Queue depth.** It reads the relay's own on-disk queue directories (`queue/`, `temp/`,
  `failed/`) and reports counts and the oldest message's age.
- **An end-to-end canary.** Every 6 hours it sends a real message to `CANARY_TO` and confirms it
  leaves the queue without landing in `failed/`, which exercises the actual Graph call rather than
  just the SMTP listener. A mailbox rule on the `[canary] smtp2graph` subject keeps the traffic out
  of the way. Shortening `CANARY_INTERVAL_SECONDS` buys a tighter detection window at the cost of
  more mail.

The exporter is a real file, not inline YAML, so it is unit-testable outside the cluster. All
three paths (delivered, rejected by Graph, relay down) were exercised against a stub SMTP server
before merge.

## Queue stall vs. stranded message

`SMTP2GraphQueueStalled` fires on any backlog older than 30 minutes and is the only alert covering
a message that smtp2graph will never retry and never move to `failed/` (three Graph error codes it
treats as permanent). See [KB-035](../troubleshooting/kb/035-smtp2graph-message-stranded-in-queue.md)
for the symptom, cause and fix; do not duplicate that runbook here.

## PodMonitor, not ServiceMonitor

The metrics sidecar is scraped by `podmonitor.yaml` rather than through its own Service. Giving it
one would push app-template past one Service on the controller, which renames the existing
`smtp2graph` Service to `smtp2graph-app`. That breaks `smtp2graph.infrastructure.svc.cluster.local`,
the address every in-cluster mail sender (pocket-id, tandoor, epicgames, scanopy) and the
LoadBalancer VIP depend on. Scraping the pod directly avoids the rename entirely.

## References

- `kubernetes/apps/infrastructure/smtp2graph/app/helmrelease.yaml`, the metrics sidecar and canary
  configuration.
- `kubernetes/apps/infrastructure/smtp2graph/app/prometheusrule.yaml`, the delivery-outcome alerts.
- `kubernetes/apps/infrastructure/smtp2graph/app/exporter/exporter.py`, the exporter and canary.
- [KB-035](../troubleshooting/kb/035-smtp2graph-message-stranded-in-queue.md), the stranded-message
  runbook.
