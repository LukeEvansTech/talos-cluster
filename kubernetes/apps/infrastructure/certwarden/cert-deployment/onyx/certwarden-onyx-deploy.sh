#!/bin/bash
# editorconfig-checker-disable-file
# (Embeds a YAML Job manifest in a heredoc; YAML uses 2-space indent and
# editorconfig-checker can't distinguish that from the shell-script body.)
#
# Certwarden calls this after certificate renewal. Deploys via the
# ghcr.io/lukeevanstech/onyx-deployer container.

set -euo pipefail

# Debug output to stderr (Certwarden captures this)
echo "DEBUG: Containerized script started at $(date)" >&2
echo "DEBUG: Environment variables:" >&2
env | grep -E "(ONYX|CERTIFICATE|PRIVATE|NAMESPACE)" | sort >&2 || echo "DEBUG: No matching env vars" >&2
echo "DEBUG: Working directory: $(pwd)" >&2
echo "DEBUG: User: $(whoami)" >&2
echo "DEBUG: Script path: $0" >&2

# Validate required environment variables FIRST (before using them with set -u)
if [[ -z "${ONYX_SWITCH:-}" ]]; then
    echo "ERROR: ONYX_SWITCH environment variable is required"
    exit 1
fi

if [[ -z "${CERTIFICATE_PEM:-}" ]]; then
    echo "ERROR: CERTIFICATE_PEM not provided by Certwarden"
    exit 1
fi

if [[ -z "${PRIVATE_KEY_PEM:-}" ]]; then
    echo "ERROR: PRIVATE_KEY_PEM not provided by Certwarden"
    exit 1
fi

NAMESPACE="${NAMESPACE:-infrastructure}"
SECRET_NAME="onyx-${ONYX_SWITCH}"

echo "=== Certwarden NVIDIA Onyx Certificate Deployment (CONTAINERIZED) ==="
echo "Certificate: ${CERTIFICATE_NAME:-unknown}"
echo "Target Onyx Switch: ${ONYX_SWITCH}"
echo "Namespace: ${NAMESPACE}"
echo "Container: ghcr.io/lukeevanstech/onyx-deployer:latest"

JOB_NAME="onyx-cert-deploy-${ONYX_SWITCH}-$(date +%s)"

CERT_SECRET_NAME="${JOB_NAME}-cert"
echo "Creating temporary secret: ${CERT_SECRET_NAME}"

kubectl create secret generic "${CERT_SECRET_NAME}" \
    -n "${NAMESPACE}" \
    --from-literal=cert.pem="${CERTIFICATE_PEM}" \
    --from-literal=key.pem="${PRIVATE_KEY_PEM}"

echo "Creating deployment Job: ${JOB_NAME}"
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: certwarden-onyx-deploy
    app.kubernetes.io/instance: ${ONYX_SWITCH}
    deployment-method: container
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 2
  template:
    spec:
      serviceAccountName: certwarden
      restartPolicy: Never
      containers:
        - name: onyx-deploy
          image: ghcr.io/lukeevanstech/onyx-deployer:latest
          imagePullPolicy: Always
          command:
            - sh
            - -c
            - |
              set -e

              # Read Onyx configuration from secret
              export ONYX_HOSTNAME=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.ONYX_HOSTNAME}' | base64 -d)
              export ONYX_USERNAME=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.ONYX_USERNAME}' | base64 -d)
              export ONYX_PASSWORD=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.ONYX_PASSWORD}' | base64 -d)
              export ONYX_CERT_NAME=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.ONYX_CERT_NAME}' | base64 -d 2>/dev/null || echo "certwarden-cert")

              echo "=== Onyx Switch Configuration ==="
              echo "Onyx Hostname: \${ONYX_HOSTNAME}"
              echo "Onyx Username: \${ONYX_USERNAME}"
              echo "Onyx Cert Name: \${ONYX_CERT_NAME}"

              echo "=== Deploying certificate using containerized approach ==="
              python3 /app/onyx_cert_updater.py \\
                --hostname "\${ONYX_HOSTNAME}" \\
                --username "\${ONYX_USERNAME}" \\
                --password "\${ONYX_PASSWORD}" \\
                --cert-name "\${ONYX_CERT_NAME}" \\
                --cert-file /certs/cert.pem \\
                --key-file /certs/key.pem \\
                --debug
          volumeMounts:
            - name: certs
              mountPath: /certs
      volumes:
        - name: certs
          secret:
            secretName: ${CERT_SECRET_NAME}
EOF

echo "Waiting for Job to complete..."
kubectl wait --for=condition=complete --timeout=5m "job/${JOB_NAME}" -n "${NAMESPACE}"

echo "=== Job Logs ==="
kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}"

JOB_STATUS=$(kubectl get job "${JOB_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')
if [[ "${JOB_STATUS}" == "True" ]]; then
    echo "✅ Certificate deployed successfully to ${ONYX_SWITCH} (containerized)"
    exit 0
else
    echo "❌ Failed to deploy certificate to ${ONYX_SWITCH} (containerized)"
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}"
    exit 1
fi
