# KB-035: smtp2graph Message Stranded in the Queue (Never Retried, Never Failed)

**Status:** Reference. Upstream behaviour of smtp2graph v1.1.5. There is no config switch for it, so
the fix is a manual re-queue. First seen 2026-09-24, when one delivery canary sat in the queue for
nine hours while every other message went through.

## Symptom

`SMTP2GraphQueueStalled` fires and stays firing. `smtp2graph_messages_queued` is a small constant
(usually 1) and `smtp2graph_oldest_queued_seconds` climbs steadily. `smtp2graph_messages_failed`
stays at 0, the canary goes on succeeding, and mail sent after the stuck message is delivered
normally. So the queue is not stalled. One file is stranded in it.

The app container's log holds exactly **one** error for the file and nothing after it, with no
retries:

```text
[MailQueue] Failed to send message "<id>.eml"
```

The reason is in the file log, not in `kubectl logs`:

```bash
kubectl -n infrastructure exec deploy/smtp2graph -c app -- \
  grep '<id>' /data/logs/error.log
```

It will be one of `Invalid content for mail`, `Access to mailbox "…" denied` or
`The message exceeds the maximum supported size`.

## Cause

smtp2graph maps three Graph error codes to a "permanent" error class:

| Graph error code | Logged as |
| --- | --- |
| `ErrorMimeContentInvalidBase64String` | `Invalid content for mail` |
| `ErrorAccessDenied` | `Access to mailbox "…" denied` |
| `ErrorMessageSizeExceeded` | `The message exceeds the maximum supported size` |

Its queue handler does not retry a permanent error, and it does not move the file to `failed/`
either. Moving to `failed/` only happens when a *retryable* error uses up `retryLimit`. The file
stays in `queue/`, and the file watcher only acts on new files, so nothing looks at it again.

Two consequences:

- `SMTP2GraphUndeliverableMail` (which counts `failed/`) **never fires** for these three errors.
  The warning-level `SMTP2GraphQueueStalled` is the only signal. `ErrorSendAsDenied`, the error
  behind the Aug 2026 outage, is not in the permanent class. It retries and ends up in `failed/`,
  so the critical alert still covers it.
- A "permanent" error is not always permanent. On 2026-09-24 Graph rejected a 454-byte, 7-bit,
  plain-text canary as invalid base64, and the same file was delivered on the first re-queue.
  Treat `Invalid content` on a small, well-formed message as a transient Graph fault.

## Fix

Look at the message before you re-send it. A real oversize or access-denied failure will fail
the same way again.

```bash
kubectl -n infrastructure exec deploy/smtp2graph -c app -- sh -c \
  'ls -la /data/mailroot/queue; grep -m4 -iE "^(From|To|Subject):" /data/mailroot/queue/*.eml'
```

To re-queue it, move the file out of `queue/` and back in, so the watcher sees a new file:

```bash
f='<id>.eml'
kubectl -n infrastructure exec deploy/smtp2graph -c app -- sh -c \
  "cd /data/mailroot && mv queue/$f temp/$f && sleep 2 && mv temp/$f queue/$f"
```

Confirm from the exporter, not from the absence of log lines (a successful send logs nothing at
the default level):

```bash
kubectl -n infrastructure exec deploy/smtp2graph -c metrics -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9184/metrics').read().decode())" \
  | grep -E '^smtp2graph_(messages|oldest)'
```

`messages_queued 0` and `messages_failed 0` mean it was delivered. If it fails again, the
message is still mail the relay has accepted, so leave it in `queue/` until you have fixed the
cause:

- **Access denied:** the Exchange RBAC-for-Applications scope on the app registration changed.
  See the `send.forceMailbox` note in `externalsecret.yaml`. Repair the scope, then re-queue.
- **Invalid content, again:** check the file for real MIME damage (truncation, 8-bit bytes in a
  7-bit part). If it looks well-formed, wait and re-queue later rather than giving up after two
  tries.
- **Size exceeded:** this is the only case that can never succeed as-is. Tell the sender, then
  delete the file.

Only delete a message once you have confirmed it can never be sent. Copy it out with
`kubectl cp` first if anyone might still want what it says.

Do **not** clear it by deleting the pod. The queue is on an `emptyDir`, so a new pod silently
drops every queued message with it.

## References

- `kubernetes/apps/infrastructure/smtp2graph/app/prometheusrule.yaml`, the delivery-outcome
  alerts and why they exist.
- `kubernetes/apps/infrastructure/smtp2graph/app/exporter/`, the queue exporter and 6-hourly
  canary.
