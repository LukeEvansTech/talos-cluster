# OIDC federation for Talos

`kubernetes/apps/network/oidc-talos` publishes this cluster's OIDC discovery document and JSON
Web Key Set so Microsoft Entra ID, or any other OIDC-aware cloud, can validate Kubernetes
service-account tokens. Only two static JSON endpoints are exposed publicly; the apiserver itself
is never reached from outside the cluster.

## Cross-repo coupling

The apiserver's `service-account-issuer` flag, set in `talos/patches/controller/cluster.yaml`,
must equal `https://oidc-talos.${SECRET_DOMAIN}`. Azure's federated credential, a `terraform-entra`
variable named `sentinel_syslog_oidc_issuer`, must point at the same URL. The variable name is a
holdover from PR #3239, which introduced the in-cluster syslog shipper to Microsoft Sentinel and
this OIDC endpoint in the same change; the federated-identity variable kept the sentinel-syslog
name even though other workloads use the same issuer now.

## From a committed JWKS to a live fetch

The app originally shipped with the cluster's public signing keys committed straight into the
ConfigMap, fetched by hand with `kubectl get --raw /openid/v1/jwks` when the app was built. PR #3263
replaced that with an init container, `fetch-jwks`, that populates an `emptyDir` from the live
apiserver before nginx starts, and a native sidecar, `refresh-jwks`, that re-fetches hourly.
A committed key block would be wrong after a fresh bootstrap that reuses the app's name, and it
would go stale if the cluster's service-account signing key ever rotated. The live fetch avoids
both. A failed refresh keeps serving the last JWKS it fetched successfully rather than failing
closed.

A follow-up, PR #3265, turned on `automountServiceAccountToken` at the pod level. app-template
disables the automount by default, so the fetch and refresh containers had no token or CA bundle
to authenticate with, and crash-looped on the first deploy. The previous pod kept serving the JWKS
throughout, because the rolling update held on the not-ready replacement, so the endpoint saw no
downtime.

## A dedicated ServiceAccount instead of the cluster-wide binding

`rbac.yaml` binds `system:service-account-issuer-discovery` to a ServiceAccount scoped to this
app, rather than relying on the cluster's built-in `system:serviceaccounts` discovery binding.
That keeps the fetch and refresh containers working even on a cluster bootstrapped from scratch,
before any cluster-wide binding exists.

## Flux postBuild and the inline shell scripts

The fetch and refresh scripts double every `$` in their `$(cat ...)` command substitutions,
because Flux's `postBuild.substitute` step runs as a text substitution over the whole rendered
manifest and would otherwise treat `$(...)` as something to expand. The scripts also address the
apiserver as `kubernetes.default.svc` rather than `${KUBERNETES_SERVICE_HOST}`: writing that
environment variable in `${...}` form would look like an unset Flux substitution variable and get
replaced with an empty string.
