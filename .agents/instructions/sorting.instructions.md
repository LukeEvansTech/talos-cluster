# Sorting instructions for all YAML files

Whenever asked to sort these files, follow these instructions:

- **Default rule**: All fields and properties should be sorted alphabetically at every level of the YAML structure, regardless of how deeply nested they are, unless a specific override rule is provided below or in other applicable instructions files.
- All YAML files should start with `---` at the top of the document.
- All documents should have a `YAML` LSP schema associated with them, if possible, to enable validation and auto-completion features in editors that support it. This is especially important for Kubernetes-related files, which should use the appropriate Kubernetes schema based on their `apiVersion` and `kind` fields. It should be below the `---` at the top of the document.

## Override rules for Kubernetes related file types

When these fields are present at the same level of a YAML structure, sort them in this order:

1. `apiVersion`
2. `kind`
3. `metadata`
4. `spec`

Within the `metadata` section, sort the items in this order:

1. `name`
2. `namespace`
3. `annotations`
4. `labels`

## HelmRelease rules for app-template

This section gives instructions specifically for HelmReleases based on the bjw-s `app-template` chart.

In this repository, app-template apps do **not** share a single chart. Each app has its own `app/ocirepository.yaml` whose `url` ends in `bjw-s-labs/helm/app-template`, and the HelmRelease references it via `spec.chartRef.kind: OCIRepository` with `spec.chartRef.name: <app>` (the app's own name). Identify an app-template HelmRelease by that per-app `OCIRepository` source — **not** by a shared `chartRef.name: app-template`.

### Sorting rules

Whenever asked to sort these files, follow these instructions.

Whenever there is an `enabled` field, it should be the first field within its section, unless a more specific rule below dictates otherwise.

Within the `spec` section, sort the items in this order (this repository orders `interval` before `chartRef`):

1. `interval`
2. `chartRef`
3. `dependsOn` (if present)
4. `install` (if present)
5. `upgrade` (if present)
6. `values`

Within the `spec.values` section, place `defaultPodOptions` first (if present), then sort all sibling keys alphabetically (e.g. `controllers`, `persistence`, `route`, `service`).

Note: Sibling keys within `persistence.*`, `service.*`, `route.*`, `configMaps.*`, etc. are NOT required to be sorted — only the keys within each individual item. For example, if `persistence` has `config`, `data`, and `tmpfs` as children, they can be in any order. Only the keys within `persistence.config`, `persistence.data`, etc. should be sorted.

**Important:** The sorting rules apply to the HelmRelease structure itself. Do NOT sort arbitrary YAML content embedded within string fields (e.g. `configMap.data.*` values containing YAML configurations).

### General pattern for section keys

Unless a more specific rule applies, order keys within any section as:

1. `annotations` (if present)
2. `labels` (if present)
3. all other keys, sorted alphabetically

### Detailed sorting rules for nested sections

Within `spec.values.controllers.*`, sort keys in this order:

1. `type` (if present, always first)
2. `annotations` (if present)
3. `labels` (if present)
4. controller-specific fields such as `cronjob` or `statefulset` (if present)
5. `pod`
6. any other fields, alphabetically — except `initContainers` then `containers`, which come last (in that order)

Within `spec.values.controllers.*.containers.*`, sort keys in this order:

1. `image`
2. any other fields, alphabetically

Within `spec.values.controllers.*.containers.resources` and `spec.values.controllers.*.initContainers.resources`, sort keys in this order:

1. `requests`
2. `limits`

Within `spec.values.service.*`, sort keys in this order:

1. `type` (if present)
2. `annotations` (if present)
3. `labels` (if present)
4. any other fields, alphabetically

Within `persistence.*`, sort keys in this order:

1. `type` (if present)
2. `annotations` (if present)
3. `labels` (if present)
4. any other fields, alphabetically — except `globalMounts` then `advancedMounts`, which come last (in that order)

### Quick reference

Before sorting, verify the chart is app-template based:

1. Check the app's `app/ocirepository.yaml` `url` ends in `bjw-s-labs/helm/app-template` (and the HelmRelease `spec.chartRef` points at that `OCIRepository`).
2. If it is not app-template, do not apply these sorting rules.

Decision tree for sorting HelmRelease fields:

```text
At spec level?
  -> interval, chartRef, dependsOn, install, upgrade, values

At spec.values level?
  -> defaultPodOptions first (if present), then alphabetical

Within controllers.*.containers.* or .initContainers.*?
  -> image first, then alphabetical

Within persistence.*, service.*, etc. siblings?
  -> No: do not sort siblings (persistence.config vs persistence.data order does not matter)
  -> Yes: sort keys within each item (type, annotations, labels, then alphabetical)
```
