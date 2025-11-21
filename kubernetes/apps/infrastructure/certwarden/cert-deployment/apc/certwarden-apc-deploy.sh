#!/bin/bash
#
# Certwarden Post-Process Script for APC NMC Certificate Deployment
#
# This script is called by Certwarden after certificate renewal.
# It creates a Kubernetes Job to deploy the certificate to the APC NMC device.
#
# Environment variables from Certwarden:
#   CERTIFICATE_NAME - Name of the certificate
#   CERTIFICATE_PEM - Certificate data (PEM format)
#   PRIVATE_KEY_PEM - Private key data (PEM format)
#   APC_HOST - Custom env var: APC host identifier (e.g., ups-main)
#   NAMESPACE - Optional: Kubernetes namespace (default: infrastructure)
#

set -euo pipefail

# Debug output to stderr (Certwarden captures this)
echo "DEBUG: Script started at $(date)" >&2
echo "DEBUG: Environment variables:" >&2
env | grep -E "(APC|CERTIFICATE|PRIVATE|NAMESPACE)" | sort >&2 || echo "DEBUG: No matching env vars" >&2
echo "DEBUG: Working directory: $(pwd)" >&2
echo "DEBUG: User: $(whoami)" >&2
echo "DEBUG: Script path: $0" >&2

# Validate required environment variables FIRST (before using them with set -u)
if [[ -z "${APC_HOST:-}" ]]; then
    echo "ERROR: APC_HOST environment variable is required" >&2
    exit 1
fi

if [[ -z "${CERTIFICATE_PEM:-}" ]]; then
    echo "ERROR: CERTIFICATE_PEM not provided by Certwarden" >&2
    exit 1
fi

if [[ -z "${PRIVATE_KEY_PEM:-}" ]]; then
    echo "ERROR: PRIVATE_KEY_PEM not provided by Certwarden" >&2
    exit 1
fi

# Now safe to use variables with set -u
echo "DEBUG: Setting NAMESPACE and SECRET_NAME..." >&2
NAMESPACE="${NAMESPACE:-infrastructure}"
SECRET_NAME="apc-${APC_HOST}"
echo "DEBUG: NAMESPACE=${NAMESPACE}, SECRET_NAME=${SECRET_NAME}" >&2

echo "=== Certwarden APC NMC Certificate Deployment ===" >&2
echo "Certificate: ${CERTIFICATE_NAME:-unknown}" >&2
echo "Target APC: ${APC_HOST}" >&2
echo "Namespace: ${NAMESPACE}" >&2

# Create a unique job name with timestamp
JOB_NAME="apc-cert-deploy-${APC_HOST}-$(date +%s)"

# Create a temporary secret for the certificate
CERT_SECRET_NAME="${JOB_NAME}-cert"
echo "Creating temporary secret: ${CERT_SECRET_NAME}" >&2

kubectl create secret generic "${CERT_SECRET_NAME}" \
    -n "${NAMESPACE}" \
    --from-literal=cert.pem="${CERTIFICATE_PEM}" \
    --from-literal=key.pem="${PRIVATE_KEY_PEM}"

# Note: Secret cleanup is handled by the Job's ownerReferences
# The secret will be garbage collected when the Job is deleted via ttlSecondsAfterFinished

# Create the deployment Job
echo "Creating deployment Job: ${JOB_NAME}" >&2
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: certwarden-apc-deploy
    app.kubernetes.io/instance: ${APC_HOST}
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 2
  template:
    spec:
      serviceAccountName: certwarden
      restartPolicy: Never
      containers:
        - name: apc-deploy
          image: docker.io/python:3.12-alpine
          command:
            - sh
            - -c
            - |
              set -e

              # Install dependencies
              apk add --no-cache curl openssh-client
              curl -LO "https://dl.k8s.io/release/\$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
              chmod +x kubectl
              mv kubectl /usr/local/bin/

              # Download and install apc-p15-tool
              echo "=== Downloading apc-p15-tool ==="
              APC_TOOL_VERSION="v1.3.3"
              curl -L "https://github.com/gregtwallace/apc-p15-tool/releases/download/\${APC_TOOL_VERSION}/apc-p15-tool-\${APC_TOOL_VERSION}_linux_amd64.tar.gz" -o /tmp/apc-p15-tool.tar.gz
              tar -xzf /tmp/apc-p15-tool.tar.gz -C /tmp
              mv /tmp/apc-p15-tool /usr/local/bin/apc-p15-tool
              chmod +x /usr/local/bin/apc-p15-tool
              apc-p15-tool version

              # Read APC configuration from secret
              export APC_HOSTNAME=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.APC_HOSTNAME}' | base64 -d)
              export APC_USERNAME=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.APC_USERNAME}' | base64 -d)
              export APC_PASSWORD=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.APC_PASSWORD}' | base64 -d)
              export APC_FINGERPRINT=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.APC_FINGERPRINT}' | base64 -d)
              export APC_INSECURE_CIPHER=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.APC_INSECURE_CIPHER}' | base64 -d || echo "false")

              echo "=== APC NMC Configuration ==="
              echo "APC Hostname: \${APC_HOSTNAME}"
              echo "APC Username: \${APC_USERNAME}"
              echo "APC Fingerprint: \${APC_FINGERPRINT}"
              echo "APC Insecure Cipher: \${APC_INSECURE_CIPHER}"

              # Build command with optional insecure cipher flag
              INSECURE_FLAG=""
              if [[ "\${APC_INSECURE_CIPHER}" == "true" ]]; then
                INSECURE_FLAG="--insecure-cipher"
                echo "⚠️  Using insecure cipher support for legacy APC devices"
              fi

              echo "=== Deploying certificate ==="
              python3 /scripts/apc-updater.py \\
                --hostname "\${APC_HOSTNAME}" \\
                --username "\${APC_USERNAME}" \\
                --password "\${APC_PASSWORD}" \\
                --fingerprint "\${APC_FINGERPRINT}" \\
                --cert-file /certs/cert.pem \\
                --key-file /certs/key.pem \\
                --apc-tool-path /usr/local/bin/apc-p15-tool \\
                \${INSECURE_FLAG} \\
                --debug
          volumeMounts:
            - name: scripts
              mountPath: /scripts
            - name: certs
              mountPath: /certs
      volumes:
        - name: scripts
          configMap:
            name: certwarden-apc-scripts
            defaultMode: 0755
        - name: certs
          secret:
            secretName: ${CERT_SECRET_NAME}
EOF

# Wait for the job to complete
echo "Waiting for Job to complete..." >&2
kubectl wait --for=condition=complete --timeout=5m "job/${JOB_NAME}" -n "${NAMESPACE}"

# Get the job logs
echo "=== Job Logs ===" >&2
kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}"

# Check if the job succeeded
JOB_STATUS=$(kubectl get job "${JOB_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')
if [[ "${JOB_STATUS}" == "True" ]]; then
    echo "✅ Certificate deployed successfully to ${APC_HOST}" >&2
    exit 0
else
    echo "❌ Failed to deploy certificate to ${APC_HOST}" >&2
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" >&2
    exit 1
fi
