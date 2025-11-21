#!/bin/bash
#
# Certwarden Post-Process Script for IPMI Certificate Deployment
#
# This script is called by Certwarden after certificate renewal.
# It deploys the certificate to the specified IPMI host using the Python ipmi-updater.
#
# Environment variables from Certwarden:
#   CERTIFICATE_NAME - Name of the certificate
#   CERTIFICATE_PEM - Certificate data (PEM format)
#   PRIVATE_KEY_PEM - Private key data (PEM format)
#   IPMI_HOST - Custom env var: IPMI host identifier (e.g., cr-storage-ipmi)
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_UPDATER="${SCRIPT_DIR}/ipmi-updater.py"

# Validate required environment variables
if [[ -z "${IPMI_HOST:-}" ]]; then
    echo "ERROR: IPMI_HOST environment variable is required"
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

# Read IPMI configuration from Kubernetes secret
# The secret name follows the pattern: ipmi-<hostname>
SECRET_NAME="ipmi-${IPMI_HOST}"
NAMESPACE="${NAMESPACE:-infrastructure}"

echo "=== Certwarden IPMI Certificate Deployment ==="
echo "Certificate: ${CERTIFICATE_NAME:-unknown}"
echo "Target IPMI: ${IPMI_HOST}"
echo "Reading config from secret: ${SECRET_NAME}"

# Fetch IPMI configuration from Kubernetes secret
IPMI_URL=$(kubectl get secret "${SECRET_NAME}" -n "${NAMESPACE}" -o jsonpath='{.data.IPMI_URL}' | base64 -d)
IPMI_MODEL=$(kubectl get secret "${SECRET_NAME}" -n "${NAMESPACE}" -o jsonpath='{.data.IPMI_MODEL}' | base64 -d)
IPMI_USERNAME=$(kubectl get secret "${SECRET_NAME}" -n "${NAMESPACE}" -o jsonpath='{.data.IPMI_USERNAME}' | base64 -d)
IPMI_PASSWORD=$(kubectl get secret "${SECRET_NAME}" -n "${NAMESPACE}" -o jsonpath='{.data.IPMI_PASSWORD}' | base64 -d)

if [[ -z "$IPMI_URL" || -z "$IPMI_MODEL" || -z "$IPMI_USERNAME" || -z "$IPMI_PASSWORD" ]]; then
    echo "ERROR: Failed to read IPMI configuration from secret ${SECRET_NAME}"
    exit 1
fi

echo "IPMI URL: ${IPMI_URL}"
echo "IPMI Model: ${IPMI_MODEL}"
echo "IPMI Username: ${IPMI_USERNAME}"

# Create temporary files for certificate and key
TEMP_DIR=$(mktemp -d)
CERT_FILE="${TEMP_DIR}/cert.pem"
KEY_FILE="${TEMP_DIR}/key.pem"

# Write certificate and key to temporary files
echo "${CERTIFICATE_PEM}" > "${CERT_FILE}"
echo "${PRIVATE_KEY_PEM}" > "${KEY_FILE}"

# Ensure cleanup on exit
trap 'rm -rf "${TEMP_DIR}"' EXIT

# Call the Python IPMI updater
echo "Deploying certificate to IPMI..."
if python3 "${PYTHON_UPDATER}" \
    --ipmi-url "${IPMI_URL}" \
    --model "${IPMI_MODEL}" \
    --username "${IPMI_USERNAME}" \
    --password "${IPMI_PASSWORD}" \
    --cert-file "${CERT_FILE}" \
    --key-file "${KEY_FILE}"; then
    echo "✅ Certificate deployed successfully to ${IPMI_HOST}"
    exit 0
else
    echo "❌ Failed to deploy certificate to ${IPMI_HOST}"
    exit 1
fi
