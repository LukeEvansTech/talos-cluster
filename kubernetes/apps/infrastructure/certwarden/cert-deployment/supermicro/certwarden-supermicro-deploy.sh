#!/bin/bash
# editorconfig-checker-disable-file
# (Embeds a YAML Job manifest in a heredoc; YAML uses 2-space indent and
# editorconfig-checker can't distinguish that from the shell-script body.)
#
# Certwarden calls this after certificate renewal. Deploys via the
# ghcr.io/lukeevanstech/supermicro-ipmi-cert container.

set -euo pipefail

# Debug output to stderr (Certwarden captures this)
echo "DEBUG: Containerized script started at $(date)" >&2
echo "DEBUG: Environment variables:" >&2
env | grep -E "(SUPERMICRO|CERTIFICATE|PRIVATE|NAMESPACE)" | sort >&2 || echo "DEBUG: No matching env vars" >&2
echo "DEBUG: Working directory: $(pwd)" >&2
echo "DEBUG: User: $(whoami)" >&2
echo "DEBUG: Script path: $0" >&2

# Validate required environment variables FIRST (before using them with set -u)
if [[ -z "${SUPERMICRO_HOST:-}" ]]; then
    echo "ERROR: SUPERMICRO_HOST environment variable is required"
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
SECRET_NAME="supermicro-${SUPERMICRO_HOST}"

echo "=== Certwarden Supermicro Certificate Deployment (CONTAINERIZED) ==="
echo "Certificate: ${CERTIFICATE_NAME:-unknown}"
echo "Target Supermicro: ${SUPERMICRO_HOST}"
echo "Namespace: ${NAMESPACE}"
echo "Container: ghcr.io/lukeevanstech/supermicro-ipmi-cert:latest"

JOB_NAME="supermicro-cert-deploy-${SUPERMICRO_HOST}-$(date +%s)"

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
    app.kubernetes.io/name: certwarden-supermicro-deploy
    app.kubernetes.io/instance: ${SUPERMICRO_HOST}
    deployment-method: container
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 2
  template:
    spec:
      serviceAccountName: certwarden
      restartPolicy: Never
      containers:
        - name: supermicro-deploy
          image: ghcr.io/lukeevanstech/supermicro-ipmi-cert@sha256:1ea09a6c81aa09ecbda43beeb7edb5df545dcd90827d3602d46cadf34016011c
          imagePullPolicy: Always
          command:
            - sh
            - -c
            - |
              set -e

              # Read IPMI configuration from secret
              export IPMI_URL=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.IPMI_URL}' | base64 -d)
              export IPMI_MODEL=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.IPMI_MODEL}' | base64 -d)
              export IPMI_USERNAME=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.IPMI_USERNAME}' | base64 -d)
              export IPMI_PASSWORD=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.IPMI_PASSWORD}' | base64 -d)

              echo "=== IPMI Configuration ==="
              echo "IPMI URL: \${IPMI_URL}"
              echo "IPMI Model: \${IPMI_MODEL}"
              echo "IPMI Username: \${IPMI_USERNAME}"

              echo "=== Deploying certificate using containerized approach ==="
              python3 /app/supermicro_ipmi_cert.py \\
                --ipmi-url "\${IPMI_URL}" \\
                --model "\${IPMI_MODEL}" \\
                --username "\${IPMI_USERNAME}" \\
                --password "\${IPMI_PASSWORD}" \\
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
    echo "✅ Certificate deployed successfully to ${SUPERMICRO_HOST} (containerized)"
    exit 0
else
    echo "❌ Failed to deploy certificate to ${SUPERMICRO_HOST} (containerized)"
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}"
    exit 1
fi
