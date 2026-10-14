# smtp2graph delivery monitoring

`smtp2graph` is the cluster's SMTP-to-Microsoft-Graph relay, in the `infrastructure` namespace
(`kubernetes/apps/infrastructure/smtp2graph`). This page covers why its monitoring measures
delivery outcome rather than reachability, and the sidecar and canary design behind that.

## Why delivery, not reachability

Between 2026-08-10 and 2026-08-19 the relay accepted mail and then failed every Graph send with
`ErrorSendAsDenied`: 353 messages lost, 2,824 errors, zero delivered. The SMTP listener stayed
healthy the whole time, so a liveness probe, a TCP check or a Gatus probe would all have stayed
green. Nothing noticed for nine days. Root cause was fixed separately, outside this repository;
the metrics sidecar and `prometheusrule.yaml` (PR #4402) exist so a failure is caught within
minutes whenever mail is flowing. During a quiet period the 6-hourly canary is the only signal,
and `SMTP2GraphCanaryFailing` does not fire until it has missed for about 14 hours.

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

The metrics sidecar is scraped by `podmonitor.yaml` rather than through its own Service. This was a
live regression, not foresight: PR #4402 first gave the sidecar its own Service, and app-template
5.x suffixes every Service on a controller the moment there is more than one, so `smtp2graph`
became `smtp2graph-app`. That broke `smtp2graph.infrastructure.svc.cluster.local`, the address
every in-cluster mail sender (pocket-id, tandoor, epicgames, scanopy) uses, and closed the
LoadBalancer VIP the NMC fleet sends through. `primary: true` does not opt out of the suffixing,
confirmed against the chart schema and by rendering it. PR #4407 fixed it by dropping the second
Service: the sidecar declares a named container port and `PodMonitor` scrapes the pod directly.

## TLS: STARTTLS offered, not required

The target state is a relay that offers STARTTLS on port 25 with a Let's Encrypt certificate for
`smtp.${SECRET_DOMAIN}` (`certificate.yaml`), the LoadBalancer's external-dns name. The certificate
is publicly trusted, so clients verify it with their stock roots and no private CA is handed out.
That only works if the client connects by that name: a client pointed at
`smtp2graph.infrastructure.svc.cluster.local` gets a name mismatch, and a client that upgrades
opportunistically (nodemailer) then fails to send.

| Sender | Target TLS | How |
| --- | --- | --- |
| Alertmanager (`email` receiver) | Required | drop `requireTLS: false` (default is true) |
| pocket-id | Required | `SMTP_TLS: starttls` |
| tandoor | Required | `EMAIL_USE_TLS: "1"` |
| scanopy | Required | lettre requires it on any port but 465; it also logs in as user `scanopy` |
| epicgames | Opportunistic | nodemailer upgrades whenever STARTTLS is offered |
| wazuh (`wazuh-maild`) | None | maild cannot do TLS |
| UPS and PDU network cards | None | configured without SSL |
| delivery canary | None | loopback inside the pod |

TLS stays optional (`secure: false`) because of the last three rows. Login stays optional too
(`requireAuth` unset); the one user, `scanopy`, exists because Scanopy's mailer always sends AUTH.
Its password is `SCANOPY_SMTP_PASSWORD` in the `scanopy` 1Password item, and its own `allowedFrom`
limits it to `scanopy@`. With TLS on, smtp2graph accepts a login only after STARTTLS.

Each sender reconciles in its own Flux Kustomization, so the switch is staged to avoid a window
where a client demands TLS the relay does not offer, or upgrades against a name the certificate
does not cover:

1. Issue the certificate, move every sender to `smtp.${SECRET_DOMAIN}` with its TLS setting
   unchanged, and put the queue on a PVC (2026-10-14). Scanopy cannot send until step 2.
2. Mount the certificate and set `secure: false`, `tlsCertPath` and `tlsKeyPath` in the relay
   config (2026-10-14). Opportunistic clients upgrade from here on.
3. Require TLS on the senders marked Required (2026-10-14).

The queue (`/data`: `queue/`, `temp/`, `failed/`) is on a 1Gi `ceph-block` PVC with a `Recreate`
strategy. Reloader restarts the pod whenever the certificate Secret changes, and the
`letsencrypt-production` issuer uses the `shortlived` profile (6-day certificates), so that is a
restart every few days; on an `emptyDir` each one would erase queued and failed mail.

Wazuh sends as `noreply@` itself. Until 2026-10-14 it sent as `wazuh@`, which is neither an alias
on the shared mailbox nor on `allowedFrom`, so every alert email was refused at `MAIL FROM` with a
550 that `wazuh-maild` never logged.

## References

- `kubernetes/apps/infrastructure/smtp2graph/app/helmrelease.yaml`, the metrics sidecar and canary
  configuration.
- `kubernetes/apps/infrastructure/smtp2graph/app/prometheusrule.yaml`, the delivery-outcome alerts.
- `kubernetes/apps/infrastructure/smtp2graph/app/exporter/exporter.py`, the exporter and canary.
- [KB-035](../troubleshooting/kb/035-smtp2graph-message-stranded-in-queue.md), the stranded-message
  runbook.
