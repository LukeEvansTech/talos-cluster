# KB-006: Checkov CKV_K8S_21 Flags Namespaced Resources Without an Explicit Namespace

**Status:** Resolved by convention — set `metadata.namespace` explicitly on every namespaced resource you author.

## Symptom

The `security-scans` workflow (Checkov) fails a PR with **CKV_K8S_21** — _"The default namespace should not be used"_ — on namespaced resources under `app/` (ConfigMaps, ServiceAccounts, PVCs, …), even though they deploy into the correct namespace at runtime.

## Cause

Checkov scans the **raw committed manifests**, _before_ Flux applies the Kustomization's `targetNamespace` (or the parent-dir `kustomization.yaml` `namespace:`). A resource with no explicit `metadata.namespace` looks like it lives in `default` to the scanner, so it gets flagged — even though `targetNamespace` sets it correctly at apply-time.

It bites namespaced **core** kinds (ConfigMaps, ServiceAccounts, PVCs, Services). Cluster-scoped kinds (ClusterRole / ClusterRoleBinding) are not flagged, and CRD instances (ExternalSecret, ToolHive `MCPServer`, …) are typically skipped — so it mainly surfaces on the core kinds.

## Fix

Set `metadata.namespace: <ns>` explicitly on every namespaced resource you author, matching the Kustomization's `targetNamespace`. It is redundant with `targetNamespace` at apply-time (and harmless), but it satisfies the raw-YAML scanner.

!!! note "Sibling gotcha: Trivy KSV0046 (wildcard RBAC)"
    For a deliberate read-only `resources: ["*"]` ClusterRole, suppress it path-scoped in `.trivyignore.yaml` with a justification rather than narrowing the role.
