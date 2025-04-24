#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "${0}")/lib/common.sh"

export LOG_LEVEL="debug"
export ROOT_DIR="$(git rev-parse --show-toplevel)"

# Talos requires the nodes to be 'Ready=False' before applying resources
function wait_for_nodes() {
    log debug "Waiting for nodes to be available"

    # Skip waiting if all nodes are 'Ready=True'
    if kubectl wait nodes --for=condition=Ready=True --all --timeout=10s &>/dev/null; then
        log info "Nodes are available and ready, skipping wait for nodes"
        return
    fi

    # Wait for all nodes to be 'Ready=False'
    until kubectl wait nodes --for=condition=Ready=False --all --timeout=10s &>/dev/null; do
        log info "Nodes are not available, waiting for nodes to be available. Retrying in 10 seconds..."
        sleep 10
    done
}

# Namespaces to be applied before the SOPS secrets are installed
function apply_namespaces() {
    log debug "Applying namespaces"

    local -r apps_dir="${ROOT_DIR}/kubernetes/apps"

    if [[ ! -d "${apps_dir}" ]]; then
        log error "Directory does not exist" "directory=${apps_dir}"
    fi

    for app in "${apps_dir}"/*/; do
        namespace=$(basename "${app}")

        # Check if the namespace resources are up-to-date
        if kubectl get namespace "${namespace}" &>/dev/null; then
            log info "Namespace resource is up-to-date" "resource=${namespace}"
            continue
        fi

        # Apply the namespace resources
        if kubectl create namespace "${namespace}" --dry-run=client --output=yaml \
            | kubectl apply --server-side --filename - &>/dev/null;
        then
            log info "Namespace resource applied" "resource=${namespace}"
        else
            log error "Failed to apply namespace resource" "resource=${namespace}"
        fi
    done
}

# SOPS secrets to be applied before the helmfile charts are installed
function apply_sops_secrets() {
    log debug "Applying secrets"

    local -r secrets=(
        # "${ROOT_DIR}/bootstrap/github-deploy-key.sops.yaml"
        "${ROOT_DIR}/kubernetes/components/common/global-vars/cluster-secrets.sops.yaml"
        "${ROOT_DIR}/kubernetes/components/common/sops/sops-age.sops.yaml"
    )

    for secret in "${secrets[@]}"; do
        if [ ! -f "${secret}" ]; then
            log warn "File does not exist" "file=${secret}"
            continue
        fi

        local resource_name=$(basename "${secret}" ".sops.yaml")
        log info "Processing secret" "resource=${resource_name}"

        # Try to decrypt and see if there are any issues
        if ! sops --decrypt "${secret}" > /dev/null 2>&1; then
            log error "Failed to decrypt secret" "resource=${resource_name}"
            sops --decrypt "${secret}" 2>&1 | head -10  # Show the first few lines of the error
            continue
        fi

        # Check if the secret resources are up-to-date, but show errors if they occur
        if ! sops exec-file "${secret}" "kubectl --namespace flux-system diff --filename {}"; then
            log info "Secret resource needs to be updated" "resource=${resource_name}"

            # Apply secret resources and capture the output
            log info "Attempting to apply secret" "resource=${resource_name}"
            if output=$(sops exec-file "${secret}" "kubectl --namespace flux-system apply --server-side --filename {}" 2>&1); then
                log info "Secret resource applied successfully" "resource=${resource_name}"
            else
                log error "Failed to apply secret resource" "resource=${resource_name}"
                echo "Error output: ${output}"
            fi
        else
            log info "Secret resource is up-to-date" "resource=${resource_name}"
        fi
    done
}


# CRDs to be applied before the helmfile charts are installed
function apply_crds() {
    log debug "Applying CRDs"

    local -r crds=(
        # No Gateway API at present
        # renovate: datasource=github-releases depName=kubernetes-sigs/gateway-api
        # https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.3.0/experimental-install.yaml
        # Prometheus Operator CRDs in Talconfig
        # renovate: datasource=github-releases depName=prometheus-operator/prometheus-operator
        # https://github.com/prometheus-operator/prometheus-operator/releases/download/v0.82.0/stripped-down-crds.yaml
        # renovate: datasource=github-releases depName=kubernetes-sigs/external-dns
        https://raw.githubusercontent.com/kubernetes-sigs/external-dns/refs/tags/v0.16.1/docs/sources/crd/crd-manifest.yaml
    )

    for crd in "${crds[@]}"; do
        if kubectl diff --filename "${crd}" &>/dev/null; then
            log info "CRDs are up-to-date" "crd=${crd}"
            continue
        fi
        if kubectl apply --server-side --filename "${crd}" &>/dev/null; then
            log info "CRDs applied" "crd=${crd}"
        else
            log error "Failed to apply CRDs" "crd=${crd}"
        fi
    done
}



# Apply Helm releases using helmfile
function apply_helm_releases() {
    log debug "Applying Helm releases with helmfile"

    local -r helmfile_file="${ROOT_DIR}/bootstrap/helmfile.yaml"

    if [[ ! -f "${helmfile_file}" ]]; then
        log error "File does not exist" "file=${helmfile_file}"
    fi

    if ! helmfile --file "${helmfile_file}" apply --hide-notes --skip-diff-on-install --suppress-diff --suppress-secrets; then
        log error "Failed to apply Helm releases"
    fi

    log info "Helm releases applied successfully"
}

# Resources to be applied before the helmfile charts are installed
function apply_resources() {
    log debug "Applying resources"

    local -r resources_file="${ROOT_DIR}/bootstrap/resources.yaml.j2"

    if ! output=$(render_template "${resources_file}") || [[ -z "${output}" ]]; then
        exit 1
    fi

    if echo "${output}" | kubectl diff --filename - &>/dev/null; then
        log info "Resources are up-to-date"
        return
    fi

    if echo "${output}" | kubectl apply --server-side --filename - &>/dev/null; then
        log info "Resources applied"
    else
        log error "Failed to apply resources"
    fi
}


# Disks in use by rook-ceph must be wiped before Rook is installed
function wipe_rook_disks() {
    log debug "Wiping Rook disks"

    # Skip disk wipe if Rook is detected running in the cluster
    # NOTE: Is there a better way to detect Rook / OSDs?
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
        log info "Attempting to wipe disk" "node=${node}" "disk=${disk}"

        # Execute the wipe command and capture both output and exit code
        # Don't redirect to /dev/null so we can see the actual output
        output=$(talosctl --nodes "${node}" wipe disk --method ZEROES "${disk}" 2>&1)
        exit_code=$?

        if [ $exit_code -eq 0 ]; then
            log info "Disk wiped successfully" "node=${node}" "disk=${disk}"
            # Print the command output for additional information
            echo "Command output: ${output}"
        else
            log error "Failed to wipe disk" "node=${node}" "disk=${disk}" "exit_code=${exit_code}"
            # Print the error output to help with debugging
            echo "Error output: ${output}"

            # Optionally, you can decide whether to continue or exit
            log warn "Continuing with next disk despite error"
            # Uncomment the following line if you want to exit on first error
            # exit $exit_code
        fi
    done
done
}


function main() {
    check_env KUBECONFIG ROOK_DISK
    check_cli helmfile jq kubectl kustomize op talosctl yq minijinja-cli

    if ! op whoami --format=json &>/dev/null; then
        log error "Failed to authenticate with 1Password CLI"
    fi

    # Apply resources and Helm releases
    wait_for_nodes
    wipe_rook_disks
    apply_crds
    apply_namespaces
    apply_sops_secrets
    apply_resources
    apply_helm_releases

    log info "Congrats! The cluster is bootstrapped and Flux is syncing the Git repository"
}

main "$@"
