#!/bin/bash
#
# Certwarden Post-Process Script for IPMI Certificate Deployment
#
# This script is called by Certwarden after certificate renewal.
# It creates a Kubernetes Job to deploy the certificate to the IPMI host.
#
# Environment variables from Certwarden:
#   CERTIFICATE_NAME - Name of the certificate
#   CERTIFICATE_PEM - Certificate data (PEM format)
#   PRIVATE_KEY_PEM - Private key data (PEM format)
#   IPMI_HOST - Custom env var: IPMI host identifier (e.g., cr-storage-ipmi)
#
# shellcheck disable=SC2157,SC2193,SC2034
# Note: Variables use $$ syntax for Kustomize escaping, shellcheck can't parse this

set -euo pipefail

# Debug output to stderr (Certwarden captures this)
echo "DEBUG: Script started at $(date)" >&2
echo "DEBUG: Environment variables:" >&2
env | grep -E "(IPMI|CERTIFICATE|PRIVATE|NAMESPACE)" | sort >&2 || echo "DEBUG: No matching env vars" >&2
echo "DEBUG: Working directory: $(pwd)" >&2
echo "DEBUG: User: $(whoami)" >&2
echo "DEBUG: Script path: $0" >&2

# Validate required environment variables FIRST (before using them with set -u)
if [[ -z "$${IPMI_HOST:-}" ]]; then
    echo "ERROR: IPMI_HOST environment variable is required"
    exit 1
fi

if [[ -z "$${CERTIFICATE_PEM:-}" ]]; then
    echo "ERROR: CERTIFICATE_PEM not provided by Certwarden"
    exit 1
fi

if [[ -z "$${PRIVATE_KEY_PEM:-}" ]]; then
    echo "ERROR: PRIVATE_KEY_PEM not provided by Certwarden"
    exit 1
fi

# Now safe to use variables with set -u
NAMESPACE="$${NAMESPACE:-infrastructure}"
SECRET_NAME="ipmi-$${IPMI_HOST}"

echo "=== Certwarden IPMI Certificate Deployment ==="
echo "Certificate: $${CERTIFICATE_NAME:-unknown}"
echo "Target IPMI: $${IPMI_HOST}"
echo "Namespace: $${NAMESPACE}"

# Create a unique job name with timestamp
JOB_NAME="ipmi-cert-deploy-$${IPMI_HOST}-$(date +%s)"

# Create a temporary secret for the certificate
CERT_SECRET_NAME="$${JOB_NAME}-cert"
echo "Creating temporary secret: $${CERT_SECRET_NAME}"

kubectl create secret generic "$${CERT_SECRET_NAME}" \
    -n "$${NAMESPACE}" \
    --from-literal=cert.pem="$${CERTIFICATE_PEM}" \
    --from-literal=key.pem="$${PRIVATE_KEY_PEM}"

# Note: Secret cleanup is handled by the Job's ownerReferences
# The secret will be garbage collected when the Job is deleted via ttlSecondsAfterFinished

# Create the deployment Job
echo "Creating deployment Job: $${JOB_NAME}"
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: $${JOB_NAME}
  namespace: $${NAMESPACE}
  labels:
    app.kubernetes.io/name: certwarden-ipmi-deploy
    app.kubernetes.io/instance: $${IPMI_HOST}
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 2
  template:
    spec:
      serviceAccountName: certwarden
      restartPolicy: Never
      containers:
        - name: ipmi-deploy
          image: docker.io/python:3.12-alpine
          command:
            - sh
            - -c
            - |
              set -e

              # Install dependencies
              apk add --no-cache curl
              curl -LO "https://dl.k8s.io/release/\$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
              chmod +x kubectl
              mv kubectl /usr/local/bin/

              # Install Python packages
              pip3 install --no-cache-dir requests pyOpenSSL

              # Read IPMI configuration from secret
              export IPMI_URL=\$(kubectl get secret $${SECRET_NAME} -n $${NAMESPACE} -o jsonpath='{.data.IPMI_URL}' | base64 -d)
              export IPMI_MODEL=\$(kubectl get secret $${SECRET_NAME} -n $${NAMESPACE} -o jsonpath='{.data.IPMI_MODEL}' | base64 -d)
              export IPMI_USERNAME=\$(kubectl get secret $${SECRET_NAME} -n $${NAMESPACE} -o jsonpath='{.data.IPMI_USERNAME}' | base64 -d)
              export IPMI_PASSWORD=\$(kubectl get secret $${SECRET_NAME} -n $${NAMESPACE} -o jsonpath='{.data.IPMI_PASSWORD}' | base64 -d)

              echo "=== IPMI Configuration ==="
              echo "IPMI URL: \$${IPMI_URL}"
              echo "IPMI Model: \$${IPMI_MODEL}"
              echo "IPMI Username: \$${IPMI_USERNAME}"

              echo "=== Deploying certificate ==="
              python3 /scripts/ipmi-updater.py \\
                --ipmi-url "\$${IPMI_URL}" \\
                --model "\$${IPMI_MODEL}" \\
                --username "\$${IPMI_USERNAME}" \\
                --password "\$${IPMI_PASSWORD}" \\
                --cert-file /certs/cert.pem \\
                --key-file /certs/key.pem
          volumeMounts:
            - name: scripts
              mountPath: /scripts
            - name: certs
              mountPath: /certs
      volumes:
        - name: scripts
          configMap:
            name: certwarden-ipmi-scripts
            defaultMode: 0755
        - name: certs
          secret:
            secretName: $${CERT_SECRET_NAME}
EOF

# Wait for the job to complete
echo "Waiting for Job to complete..."
kubectl wait --for=condition=complete --timeout=5m "job/$${JOB_NAME}" -n "$${NAMESPACE}"

# Get the job logs
echo "=== Job Logs ==="
kubectl logs "job/$${JOB_NAME}" -n "$${NAMESPACE}"

# Check if the job succeeded
JOB_STATUS=$(kubectl get job "$${JOB_NAME}" -n "$${NAMESPACE}" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')
if [[ "$${JOB_STATUS}" == "True" ]]; then
    echo "✅ Certificate deployed successfully to $${IPMI_HOST}"
    exit 0
else
    echo "❌ Failed to deploy certificate to $${IPMI_HOST}"
    kubectl logs "job/$${JOB_NAME}" -n "$${NAMESPACE}"
    exit 1
fi
