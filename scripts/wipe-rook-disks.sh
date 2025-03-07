#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "${0}")/lib/common.sh"

export LOG_LEVEL="debug"
export ROOT_DIR="$(git rev-parse --show-toplevel)"

# Disks in use by rook-ceph must be wiped before Rook is installed
function wipe_rook_disks() {
    log debug "Wiping Rook disks"

    # Skip disk wipe if Rook is detected running in the cluster
    # TODO: Is there a better way to detect Rook / OSDs?
    if kubectl --namespace rook-ceph get kustomization rook-ceph &>/dev/null; then
        log warn "Rook is detected running in the cluster, skipping disk wipe"
        return
    fi

    if ! nodes=$(talosctl config info --output json 2>/dev/null | jq --exit-status --raw-output '.nodes | join(" ")') || [[ -z "${nodes}" ]]; then
        log error "No Talos nodes found"
    fi

    log debug "Talos nodes discovered" "nodes=${nodes}"

    # Wipe disks on each node that match the ROOK_DISK environment variable
    for node in ${nodes}; do
        if ! disks=$(talosctl --nodes "${node}" get disk --output json 2>/dev/null \
            | jq --exit-status --raw-output --slurp '. | map(select(.spec.model == env.ROOK_DISK) | .metadata.id) | join(" ")') || [[ -z "${nodes}" ]];
        then
            log error "No disks found" "node=${node}" "model=${ROOK_DISK}"
        fi

        log debug "Talos node and disk discovered" "node=${node}" "disks=${disks}"

        # Wipe each disk on the node
        for disk in ${disks}; do
            if talosctl --nodes "${node}" wipe disk "${disk}" &>/dev/null; then
                log info "Disk wiped" "node=${node}" "disk=${disk}"
            else
                log error "Failed to wipe disk" "node=${node}" "disk=${disk}"
            fi
        done
    done
}

# Check if ROOK_DISK is set
if [[ -z "${ROOK_DISK:-}" ]]; then
    log error "ROOK_DISK environment variable is not set"
    exit 1
fi

# Run the function
wipe_rook_disks

# export ROOK_DISK="INTEL SSDPE21D015TA"
# ./wipe_rook_disks.sh wipe_rook_disks
