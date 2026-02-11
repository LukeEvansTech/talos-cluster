#!/usr/bin/env bash
# Discover Shelly devices via mDNS and add them to 1Password
# Usage: ./shelly-discover.sh [--dry-run]
set -euo pipefail

OP_VAULT="Talos"
OP_ITEM="Shelly Exporter"
MDNS_TIMEOUT=5
QUERY_TIMEOUT=3
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# Discover Shelly devices via mDNS
echo "Discovering Shelly devices via mDNS (${MDNS_TIMEOUT}s)..."
mapfile -t INSTANCES < <(
  timeout "$MDNS_TIMEOUT" dns-sd -B _shelly._tcp local 2>/dev/null \
    | awk '/_shelly._tcp\./ {print $NF}' \
    | sort -u
)
echo "Found ${#INSTANCES[@]} devices"

# Resolve each device and query its info
declare -A DEVICES  # MAC -> IP
ERRORS=()

for instance in "${INSTANCES[@]}"; do
  hostname="${instance}.local"

  # Resolve IP
  ip=$(python3 -c "
import socket
try:
    print(socket.gethostbyname('${hostname}'))
except Exception:
    print('')
" 2>/dev/null)

  if [[ -z "$ip" ]]; then
    ERRORS+=("RESOLVE_FAIL: ${instance}")
    continue
  fi

  # Query device info
  info=$(curl -s --connect-timeout "$QUERY_TIMEOUT" "http://${ip}/rpc/Shelly.GetDeviceInfo" 2>/dev/null || true)
  if [[ -z "$info" ]]; then
    ERRORS+=("QUERY_FAIL: ${instance} (${ip})")
    continue
  fi

  mac=$(echo "$info" | python3 -c "import json,sys; print(json.load(sys.stdin).get('mac',''))" 2>/dev/null)
  name=$(echo "$info" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('name') or d.get('id',''))" 2>/dev/null)
  app=$(echo "$info" | python3 -c "import json,sys; print(json.load(sys.stdin).get('app',''))" 2>/dev/null)
  gen=$(echo "$info" | python3 -c "import json,sys; print(json.load(sys.stdin).get('gen',''))" 2>/dev/null)

  if [[ -n "$mac" ]]; then
    DEVICES["$mac"]="$ip"
    printf "  %-20s %-18s %-16s Gen%-2s %s\n" "$mac" "$ip" "$app" "$gen" "$name"
  fi
done

echo ""
echo "=== Summary ==="
echo "Discovered: ${#DEVICES[@]} devices"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
  echo "Errors: ${#ERRORS[@]}"
  printf "  %s\n" "${ERRORS[@]}"
fi
echo ""

if [[ ${#DEVICES[@]} -eq 0 ]]; then
  echo "No devices found. Exiting."
  exit 1
fi

if $DRY_RUN; then
  echo "[DRY RUN] Would create/update 1Password item '${OP_ITEM}' in vault '${OP_VAULT}' with ${#DEVICES[@]} fields"
  for mac in $(echo "${!DEVICES[@]}" | tr ' ' '\n' | sort); do
    echo "  Field: ${mac} = ${DEVICES[$mac]}"
  done
  exit 0
fi

# Check if 1Password item exists
if op item get "$OP_ITEM" --vault "$OP_VAULT" &>/dev/null; then
  echo "Updating existing 1Password item '${OP_ITEM}'..."
  ASSIGNMENTS=()
  for mac in "${!DEVICES[@]}"; do
    ASSIGNMENTS+=("${mac}=${DEVICES[$mac]}")
  done
  op item edit "$OP_ITEM" --vault "$OP_VAULT" "${ASSIGNMENTS[@]}"
else
  echo "Creating 1Password item '${OP_ITEM}'..."
  ASSIGNMENTS=()
  for mac in "${!DEVICES[@]}"; do
    ASSIGNMENTS+=("${mac}=${DEVICES[$mac]}")
  done
  op item create --category="Secure Note" --title="$OP_ITEM" --vault "$OP_VAULT" "${ASSIGNMENTS[@]}"
fi

echo ""
echo "Done! ${#DEVICES[@]} devices added to 1Password item '${OP_ITEM}'"
echo ""
echo "Next steps:"
echo "  1. Verify in 1Password: op item get '${OP_ITEM}' --vault '${OP_VAULT}'"
echo "  2. The ExternalSecret will sync these to the cluster automatically"
