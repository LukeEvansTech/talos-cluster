# KB-021: Grafana Dashboard Panels All Blank ("Datasource … was not found")

**Status:** Convention bug. Fix is a one-character case change; one upstream-import variant
needs vendoring.

## Symptom

Every panel on one `GrafanaDashboard` renders **"No data"** / **"Datasource Prometheus was not
found"**, and any `label_values()` template variable returns empty — cascading the whole
dashboard to blank. Other dashboards on the same Grafana work fine.

## Cause

Grafana resolves a panel's datasource ref **by uid first, then falls back to name
(case-sensitive)**. On this cluster:

- The Prometheus `GrafanaDatasource` is **named `prometheus`** (lowercase) with an
  operator-generated random uid, defined in
  `kubernetes/apps/observability/grafana/instance/grafanadatasource.yaml`.
- The grafana-operator writes `GrafanaDashboard.spec.datasources[].datasourceName` **literally**
  as each panel's datasource `uid` — it does **not** look up the real uid. Grafana then matches
  that literal against the datasource's **name**.

So `datasourceName: prometheus` resolves by name ✓, but `datasourceName: Prometheus` (capital)
matches neither a uid nor the name → every panel blanks. (This was the May/June 2026
"blank dashboards" bug across several shipped dashboards; the already-working dashboards proved
lowercase is the convention.)

**Second variant — `url:`-imported upstream JSON.** Some imported dashboards hardcode a
`$datasource` **template variable** value (e.g. "Prometheus") *inside the JSON*, which no
`datasources` remap can override. Those must be **vendored locally** (configMapRef) with the var
pinned to `prometheus`.

## Fix

- Set `datasourceName: prometheus` (lowercase) in the CR spec — this reconciles **immediately**
  via Flux.
- For url-imported dashboards that pin `$datasource` internally: **vendor** the JSON locally and
  pin the template var to `prometheus`. Also strip the upstream author's **saved variable
  defaults** — a saved host default like `esxhost=<a LAN IP>` trips the internal-identifier
  guard, and codespell may flag real typos plus schema keys (e.g. `showIn` → whitelist in
  `.github/linters/.codespellrc`).
- Confirm the fix: the browser console shows `Datasource Prometheus was not found`, or
  `GET /api/dashboards/uid/<uid>` → each panel's `datasource.uid` should read `prometheus`, not
  `Prometheus`.

## How to recognise fast

- **Operator resync caveat:** with `disableNameSuffixHash: true` the configMap name is stable,
  so editing **only the dashboard JSON** (not the CR spec) does **not** trigger a re-sync until
  `resyncPeriod` (1h). Force it:

  ```sh
  kubectl rollout restart deploy/grafana-operator -n observability
  ```

  CR **spec** changes (like the `datasourceName` fix) reconcile immediately via Flux — only raw
  JSON/configMap edits need the restart.
- **Multi-value template vars** that cleared to the wrong default (e.g. a cluster picker landing
  on a single-host cluster with no perf metrics → N/A panels): set `includeAll: true` + default
  `All` so the landing view aggregates.

## References

- grafana-operator dashboards: <https://grafana.github.io/grafana-operator/docs/dashboards/>
- Related: dashboard folder placement and exporter job-pinning live alongside this in the
  observability stack.
