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

        log info "Processing namespace directory" "namespace=${namespace}"

        # Build and apply namespace using kustomize
        if kustomize build "${app}" | yq eval 'select(.kind == "Namespace")' | \
            kubectl diff --filename - &>/dev/null;
        then
            log info "Namespace resource is up-to-date" "resource=${namespace}"
            continue
        fi

        # Apply the namespace resources
        if kustomize build "${app}" | yq eval 'select(.kind == "Namespace")' | \
            kubectl apply --server-side --field-manager=bootstrap --force-conflicts --filename - &>/dev/null;
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
            if output=$(sops exec-file "${secret}" "kubectl --namespace flux-system apply --server-side --field-manager=bootstrap --filename {}" 2>&1); then
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
    log info "Applying CRDs"

    local -r crds=(
        # renovate: datasource=github-releases depName=kubernetes-sigs/external-dns
        https://raw.githubusercontent.com/kubernetes-sigs/external-dns/refs/tags/v0.19.0/config/crd/standard/dnsendpoints.externaldns.k8s.io.yaml
        # renovate: datasource=github-releases depName=kubernetes-sigs/gateway-api
        https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.4.0/experimental-install.yaml
        # renovate: datasource=helm depName=envoyproxy/gateway-helm
        https://github.com/envoyproxy/gateway/releases/download/v1.5.4/install.yaml
        # renovate: datasource=github-releases depName=prometheus-operator/prometheus-operator
        https://github.com/prometheus-operator/prometheus-operator/releases/download/v0.86.2/stripped-down-crds.yaml
    )

    for crd in "${crds[@]}"; do
        if kubectl diff --filename "${crd}" &>/dev/null; then
            log info "CRDs are up-to-date" "crd" "${crd}"
            continue
        fi
        if ! kubectl apply --server-side --field-manager=bootstrap --filename "${crd}" &>/dev/null; then
            log fatal "Failed to apply CRDs" "crd" "${crd}"
        fi
        log info "CRDs applied" "crd" "${crd}"
    done
}

# Sync Helm releases
function sync_helm_releases() {
    log debug "Syncing Helm releases"

    local -r helmfile_file="${ROOT_DIR}/bootstrap/helmfile.yaml"

    if [[ ! -f "${helmfile_file}" ]]; then
        log error "File does not exist" "file=${helmfile_file}"
    fi

    if ! helmfile --file "${helmfile_file}" sync --hide-notes; then
        log error "Failed to sync Helm releases"
    fi

    log info "Helm releases synced successfully"
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

    if echo "${output}" | kubectl apply --server-side --field-manager=bootstrap --filename - &>/dev/null; then
        log info "Resources applied"
    else
        log error "Failed to apply resources"
    fi
}

function main() {
    check_env KUBECONFIG
    check_cli helmfile jq kubectl kustomize op talosctl yq minijinja-cli

    if ! op whoami --format=json &>/dev/null; then
        log error "Failed to authenticate with 1Password CLI"
    fi

    # Apply resources and Helm releases
    wait_for_nodes
    apply_crds
    apply_namespaces
    apply_sops_secrets
    apply_resources
    sync_helm_releases

    log info "Congrats! The cluster is bootstrapped and Flux is syncing the Git repository"
}

main "$@"
