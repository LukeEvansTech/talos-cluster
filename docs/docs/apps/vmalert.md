# vmalert

`kubernetes/apps/observability/vmalert` runs one vmalert (chart `victoria-metrics-alert`, no
operator) that evaluates LogsQL rules against [VictoriaLogs](victoria-logs.md) and sends firing
alerts to the Alertmanager from `kube-prometheus-stack`. It is the alerting path for apps that log
their work but export no metrics, the absence-of-success pattern that AGENTS.md asks for.

## Adding a rule

Rules live in the HelmRelease under `server.config.alerts.groups`. A group needs `type: vlogs`.

- **Stream filter, not pod name.** Match `{kubernetes.pod_namespace="...",kubernetes.pod_name=~"app-server-.+"}`;
  the pod name changes on every rollout.
- **Absence needs `stats`.** A query with no match returns no rows, so no alert. `| stats count() as c | filter c:0`
  returns one row with `c=0` when the window is empty.
- **Labels.** Set `severity` and `namespace` so the existing Alertmanager routes apply (`warning` pushes once, then daily).
- **No unit test.** `vmalert-tool unittest` replays PromQL series and cannot evaluate LogsQL, so check a new
  rule against a window where the failure really happened:

  ```bash
  kubectl get --raw "/api/v1/namespaces/observability/services/victoria-logs-server:9428/proxy/select/logsql/query?query=<urlencoded>"
  ```

## Checking it

```bash
kubectl get --raw "/api/v1/namespaces/observability/services/vmalert-server:8880/proxy/api/v1/rules"
```

Each rule reports `state`, `lastError` and `lastEvaluation`; an empty `lastError` means the query ran.
