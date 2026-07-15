set default-list
set default-script
set lazy
set positional-arguments := true
set quiet := true
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']

[doc('Generate Talos configuration with talhelper (secrets from 1Password)')]
gen-config:
    just log info "Generating Talos configuration..." "stage" "{{ recipe_name() }}"
    TALSECRET=$(mktemp)
    trap 'rm -f "${TALSECRET}"' EXIT
    op document get talsecret --vault Talos > "${TALSECRET}"
    # LUKS2 slot-1 fallback passphrase, substituted into talconfig.yaml as ${TALOS_LUKS_FALLBACK}.
    # Never committed: this repo is public and the rendered configs are gitignored.
    export TALOS_LUKS_FALLBACK=$(op read "op://Talos/talos-luks-fallback/password")
    if [[ -z "${TALOS_LUKS_FALLBACK}" ]]; then
        just log error "TALOS_LUKS_FALLBACK is empty — refusing to generate config without the LUKS fallback key" "stage" "{{ recipe_name() }}"
        exit 1
    fi
    talhelper genconfig --config-file "{{ source_directory() }}/talconfig.yaml" --secret-file "${TALSECRET}" --out-dir "{{ source_directory() }}/clusterconfig"

[doc('Generate secret if missing (stored as the talsecret document in 1Password)')]
gen-secret:
    if op document get talsecret --vault Talos > /dev/null 2>&1; then
        just log info "Talos secret already exists in 1Password" "stage" "{{ recipe_name() }}"
    else
        just log info "Generating Talos secret..." "stage" "{{ recipe_name() }}"
        TALSECRET=$(mktemp)
        trap 'rm -f "${TALSECRET}"' EXIT
        talhelper gensecret > "${TALSECRET}"
        op document create "${TALSECRET}" --vault Talos --title talsecret --file-name talsecret.yaml > /dev/null
    fi

[doc('Apply Talos config to a specific node')]
apply-node node *args:
    just log info "Applying Talos config to node..." "stage" "{{ recipe_name() }}" "node" "{{ node }}"
    export TALOS_LUKS_FALLBACK=$(op read "op://Talos/talos-luks-fallback/password")
    talhelper gencommand apply --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --node {{ node }} --extra-flags "{{ args }}" | bash

[confirm('Upgrade Talos on node? [y|N]')]
[doc('Upgrade Talos on a specific node')]
upgrade-node node:
    just log info "Upgrading Talos on node..." "stage" "{{ recipe_name() }}" "node" "{{ node }}"
    export TALOS_LUKS_FALLBACK=$(op read "op://Talos/talos-luks-fallback/password")
    TALOS_IMAGE=$(yq -r -e '.nodes[] | select(.ipAddress == "{{ node }}") | .talosImageURL' "{{ source_directory() }}/talconfig.yaml")
    TALOS_VERSION=$(yq -r -e '.talosVersion' "{{ source_directory() }}/talenv.yaml")
    talhelper gencommand upgrade --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --node {{ node }} --extra-flags "--image=${TALOS_IMAGE}:${TALOS_VERSION} --timeout=10m" | bash

[confirm('Upgrade Kubernetes on the cluster? [y|N]')]
[doc('Upgrade Kubernetes version')]
upgrade-k8s:
    just log info "Upgrading Kubernetes..." "stage" "{{ recipe_name() }}"
    export TALOS_LUKS_FALLBACK=$(op read "op://Talos/talos-luks-fallback/password")
    K8S_VERSION=$(yq -r -e '.kubernetesVersion' "{{ source_directory() }}/talenv.yaml")
    talhelper gencommand upgrade-k8s --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --extra-flags "--to ${K8S_VERSION}" | bash

[doc('Reset cluster nodes to maintenance mode')]
reset:
    gum confirm "This will destroy your cluster and reset the nodes back to maintenance mode. Continue?" --default=false || exit 0
    just log warn "Resetting cluster..." "stage" "{{ recipe_name() }}"
    export TALOS_LUKS_FALLBACK=$(op read "op://Talos/talos-luks-fallback/password")
    talhelper gencommand reset --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --extra-flags "--reboot --system-labels-to-wipe STATE --system-labels-to-wipe EPHEMERAL --graceful=false --wait=false" | bash
