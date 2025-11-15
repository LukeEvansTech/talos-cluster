#!/usr/bin/env bash
set -euo pipefail

# Script to store Talos certificate components in 1Password for etcd-defrag CronJob
# This extracts CA, CRT, and KEY from talosconfig and stores them in 1Password

TALOSCONFIG_FILE="${TALOSCONFIG:-talos/clusterconfig/talosconfig}"
VAULT_NAME="Talos"
ITEM_NAME="talos"

echo "=========================================="
echo "1Password Talos Secrets Setup"
echo "=========================================="
echo ""

# Check if op CLI is available
if ! command -v op &> /dev/null; then
    echo "ERROR: 1Password CLI (op) not found"
    echo "Install it from: https://developer.1password.com/docs/cli/get-started/"
    exit 1
fi

# Check if authenticated
if ! op whoami &> /dev/null; then
    echo "ERROR: Not authenticated with 1Password CLI"
    echo "Run: eval \$(op signin)"
    exit 1
fi

# Check if yq is available
if ! command -v yq &> /dev/null; then
    echo "ERROR: yq not found (required to parse YAML)"
    echo "Install it with: brew install yq (or equivalent for your OS)"
    exit 1
fi

# Check if talosconfig exists
if [[ ! -f "$TALOSCONFIG_FILE" ]]; then
    echo "ERROR: talosconfig file not found at: $TALOSCONFIG_FILE"
    echo "Generate it with: just talos gen-config"
    exit 1
fi

echo "✓ Prerequisites check passed"
echo ""

# Extract certificate components from talosconfig
echo "Extracting certificate components from talosconfig..."
TALOS_CA=$(yq eval '.contexts.kubernetes.ca' "$TALOSCONFIG_FILE")
TALOS_CRT=$(yq eval '.contexts.kubernetes.crt' "$TALOSCONFIG_FILE")
TALOS_KEY=$(yq eval '.contexts.kubernetes.key' "$TALOSCONFIG_FILE")

if [[ -z "$TALOS_CA" ]] || [[ "$TALOS_CA" == "null" ]]; then
    echo "ERROR: Could not extract CA from talosconfig"
    exit 1
fi

if [[ -z "$TALOS_CRT" ]] || [[ "$TALOS_CRT" == "null" ]]; then
    echo "ERROR: Could not extract CRT from talosconfig"
    exit 1
fi

if [[ -z "$TALOS_KEY" ]] || [[ "$TALOS_KEY" == "null" ]]; then
    echo "ERROR: Could not extract KEY from talosconfig"
    exit 1
fi

echo "✓ Successfully extracted certificate components"
echo "  - CA:  ${#TALOS_CA} bytes"
echo "  - CRT: ${#TALOS_CRT} bytes"
echo "  - KEY: ${#TALOS_KEY} bytes"
echo ""

# Check if the item already exists
echo "Checking if item '$ITEM_NAME' exists in vault '$VAULT_NAME'..."
if op item get "$ITEM_NAME" --vault "$VAULT_NAME" &> /dev/null; then
    echo "✓ Item exists, updating fields..."

    # Update existing item with new fields
    op item edit "$ITEM_NAME" \
        --vault "$VAULT_NAME" \
        "TALOS_CA[password]=$TALOS_CA" \
        "TALOS_CRT[password]=$TALOS_CRT" \
        "TALOS_KEY[password]=$TALOS_KEY"

    echo "✓ Updated existing item with certificate components"
else
    echo "Item does not exist, creating new item..."

    # Create new item with the fields
    op item create \
        --category "Secure Note" \
        --title "$ITEM_NAME" \
        --vault "$VAULT_NAME" \
        "TALOS_CA[password]=$TALOS_CA" \
        "TALOS_CRT[password]=$TALOS_CRT" \
        "TALOS_KEY[password]=$TALOS_KEY"

    echo "✓ Created new item with certificate components"
fi

echo ""
echo "=========================================="
echo "Success!"
echo "=========================================="
echo ""
echo "The following fields have been stored in 1Password:"
echo "  Vault: $VAULT_NAME"
echo "  Item:  $ITEM_NAME"
echo "  Fields:"
echo "    - TALOS_CA"
echo "    - TALOS_CRT"
echo "    - TALOS_KEY"
echo ""
echo "The etcd-defrag ExternalSecret will automatically sync these"
echo "to Kubernetes once you deploy the CronJob."
echo ""
echo "Next steps:"
echo "  1. Merge the etcd-defrag PR"
echo "  2. Wait for Flux to reconcile"
echo "  3. Verify: kubectl get externalsecret -n kube-system etcd-defrag"
