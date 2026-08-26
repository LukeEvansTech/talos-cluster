#!/bin/bash
# editorconfig-checker-disable-file
# (Embeds a YAML Job manifest in a heredoc; YAML uses 2-space indent and
# editorconfig-checker can't distinguish that from the shell-script body.)
#
# Certwarden Post-Process Script for Brother Printer Certificate Deployment
#
# This script is called by Certwarden after certificate renewal.
# It creates a Kubernetes Job to deploy the certificate to the Brother printer.
#
# Environment variables from Certwarden:
#   CERTIFICATE_NAME - Name of the certificate
#   CERTIFICATE_PEM - Certificate data (PEM format)
#   PRIVATE_KEY_PEM - Private key data (PEM format)
#   BROTHER_HOST - Custom env var: Brother printer host identifier (e.g., r-fw-core)
#   NAMESPACE - Optional: Kubernetes namespace (default: infrastructure)
#

set -euo pipefail

# Debug output to stderr (Certwarden captures this)
echo "DEBUG: Script started at $(date)" >&2
echo "DEBUG: Environment variables:" >&2
env | grep -E "(BROTHER|CERTIFICATE|PRIVATE|NAMESPACE)" | sort >&2 || echo "DEBUG: No matching env vars" >&2
echo "DEBUG: Working directory: $(pwd)" >&2
echo "DEBUG: User: $(whoami)" >&2
echo "DEBUG: Script path: $0" >&2

# Validate required environment variables FIRST (before using them with set -u)
if [[ -z "${BROTHER_HOST:-}" ]]; then
    echo "ERROR: BROTHER_HOST environment variable is required"
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

# Now safe to use variables with set -u
NAMESPACE="${NAMESPACE:-infrastructure}"
SECRET_NAME="brother-${BROTHER_HOST}"

echo "=== Certwarden Brother Printer Certificate Deployment ==="
echo "Certificate: ${CERTIFICATE_NAME:-unknown}"
echo "Target Brother Printer: ${BROTHER_HOST}"
echo "Namespace: ${NAMESPACE}"

# Create a unique job name with timestamp
JOB_NAME="brother-cert-deploy-${BROTHER_HOST}-$(date +%s)"

# Create a temporary secret for the certificate
CERT_SECRET_NAME="${JOB_NAME}-cert"
echo "Creating temporary secret: ${CERT_SECRET_NAME}"

kubectl create secret generic "${CERT_SECRET_NAME}" \
    -n "${NAMESPACE}" \
    --from-literal=cert.pem="${CERTIFICATE_PEM}" \
    --from-literal=key.pem="${PRIVATE_KEY_PEM}"

# The Secret is adopted by the Job further down, once the Job exists and has
# a UID to point at. It cannot be created with an ownerReference here
# because the owner does not exist yet, and a stale/incorrect ownerReference
# would have the Secret garbage-collected immediately.

# Create the deployment Job
echo "Creating deployment Job: ${JOB_NAME}"
cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: certwarden-brother-deploy
    app.kubernetes.io/instance: ${BROTHER_HOST}
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 2
  template:
    spec:
      serviceAccountName: certwarden
      restartPolicy: Never
      containers:
        - name: brother-deploy
          image: docker.io/library/alpine:3.23.3@sha256:25109184c71bdad752c8312a8623239686a9a2071e8825f20acb8f2198c3f659
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

              # Download and install brother-cert
              echo "=== Downloading brother-cert ==="
              BROTHER_CERT_VERSION="v1.0.0"
              curl -L "https://github.com/gregtwallace/brother-cert/releases/download/\${BROTHER_CERT_VERSION}/brother-cert_linux_amd64" -o /usr/local/bin/brother-cert
              chmod +x /usr/local/bin/brother-cert
              brother-cert --version || echo "Brother-cert installed"

              # Read Brother printer configuration from secret
              export BROTHER_CERT_HOSTNAME=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.BROTHER_HOSTNAME}' | base64 -d)
              export BROTHER_CERT_PASSWORD=\$(kubectl get secret ${SECRET_NAME} -n ${NAMESPACE} -o jsonpath='{.data.BROTHER_PASSWORD}' | base64 -d)

              echo "=== Brother Printer Configuration ==="
              echo "Brother Hostname: \${BROTHER_CERT_HOSTNAME}"

              echo "=== Deploying certificate ==="
              brother-cert \\
                --hostname "\${BROTHER_CERT_HOSTNAME}" \\
                --password "\${BROTHER_CERT_PASSWORD}" \\
                --keyfile /certs/key.pem \\
                --certfile /certs/cert.pem
          volumeMounts:
            - name: certs
              mountPath: /certs
      volumes:
        - name: certs
          secret:
            secretName: ${CERT_SECRET_NAME}
EOF

# Make the Job the owner of the cert Secret so the two are deleted together.
# blockOwnerDeletion is false so the Secret can never hold up deletion of
# the Job, and a failure here is not fatal: the deployment itself has
# already been submitted, and a Secret that outlives its Job is the old
# (bad) behaviour rather than a new one.
echo "Binding Secret ${CERT_SECRET_NAME} to Job ${JOB_NAME} for cleanup"
JOB_UID=$(kubectl get job "${JOB_NAME}" -n "${NAMESPACE}" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)
if [[ -n "${JOB_UID}" ]]; then
    kubectl patch secret "${CERT_SECRET_NAME}" -n "${NAMESPACE}" --type merge -p \
        "{\"metadata\":{\"ownerReferences\":[{\"apiVersion\":\"batch/v1\",\"kind\":\"Job\",\"name\":\"${JOB_NAME}\",\"uid\":\"${JOB_UID}\",\"controller\":false,\"blockOwnerDeletion\":false}]}}" ||
        echo "WARNING: could not set ownerReference on ${CERT_SECRET_NAME}; it will need manual cleanup"
else
    echo "WARNING: could not read Job UID; ${CERT_SECRET_NAME} will need manual cleanup"
fi

# Wait for the job to complete
echo "Waiting for Job to complete..."
kubectl wait --for=condition=complete --timeout=5m "job/${JOB_NAME}" -n "${NAMESPACE}"

# Get the job logs
echo "=== Job Logs ==="
kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}"

# Check if the job succeeded
JOB_STATUS=$(kubectl get job "${JOB_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')
if [[ "${JOB_STATUS}" == "True" ]]; then
    echo "✅ Certificate deployed successfully to ${BROTHER_HOST}"
    exit 0
else
    echo "❌ Failed to deploy certificate to ${BROTHER_HOST}"
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}"
    exit 1
fi
