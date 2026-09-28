# CertWarden

CertWarden issues and renews certificates and pushes them to devices that cannot fetch one
themselves, in the `infrastructure` namespace (`kubernetes/apps/infrastructure/certwarden`).
Each supported device has its own post-processing script that CertWarden calls after a renewal;
the script spawns a one-shot Kubernetes Job to deploy the certificate.

## kubectl needs its own RBAC

Post-process scripts run kubectl in-pod to create a Secret and spawn a deploy Job.
app-template disables `automountServiceAccountToken` by default, which would strand kubectl on
`localhost:8080`, so it is mounted back explicitly.

## ConfigMap as a direct resource

The Onyx and Supermicro script ConfigMaps are created as a direct resource
(`scripts-configmap.yaml`) rather than through `configMapGenerator`, because Kustomize's own
variable expansion would blank the scripts' `${VAR}` placeholders.

## Deploy failure observability

A failed deploy Job used to be invisible: it only left a line in Certwarden's own log, and one
device served an expired certificate for months before anyone noticed (#4052).
`CertDeployJobFailed` alerts on it; `for: 1m` fits inside the roughly 5-minute window
`ttlSecondsAfterFinished: 300` leaves before kube-state-metrics stops reporting the failed Job.

## Secret and Job cleanup ordering

Each deploy Job creates a temporary Secret holding the certificate and key before the Job
itself exists, so the Secret cannot carry an `ownerReference` at creation: the Job adopts it
further down, once the Job exists and has a UID to point at. `blockOwnerDeletion` is false, so a
failed patch here leaves a stray Secret rather than blocking deletion of the Job. Binding the
Job as the Secret's owner this way was added in #4025.

## Device credentials

### APC

The 1Password item's field names (`IP_Address`, `username`, `password`, ...) do not match the
`APC_*` keys the deploy Job reads. A bare `dataFrom.extract` left every key the Job needed empty
(`APC_HOSTNAME is empty`, #4052), so the ExternalSecret maps the fields explicitly.
`APC_FINGERPRINT` is the NMC's SSH host key fingerprint; `APC_INSECURE_CIPHER` is `"true"` for
legacy cryptlib SSH devices. To read the SSH fingerprint:

```bash
ssh -o KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1 \
    -o HostKeyAlgorithms=+ssh-rsa \
    -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    -v apc@<hostname> exit 2>&1 | grep "Server host key"
```

### Onyx

Required 1Password fields: `ONYX_HOSTNAME`, `ONYX_USERNAME`, `ONYX_PASSWORD`. Optional:
`ONYX_CERT_NAME` (defaults to `custom-cert`).

### Brother printer

Required 1Password fields: `BROTHER_HOSTNAME`, `BROTHER_PASSWORD`. The cert tool needs an RSA
key (not ECDSA, 2048-bit recommended) and a certificate that carries a Common Name.

## References

- `kubernetes/apps/infrastructure/certwarden/app/helmrelease.yaml`, the shared kubectl tooling
  and per-device script mounts.
- `kubernetes/apps/infrastructure/certwarden/cert-deployment/prometheusrule.yaml`, the deploy
  failure alert.
- `kubernetes/apps/infrastructure/certwarden/cert-deployment/{apc,onyx,brother,supermicro}/`,
  the per-device ExternalSecrets and deploy scripts.
