set lazy
set positional-arguments := true
set quiet := true
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']

[private]
default:
    just -l talos

[doc('Generate Talos configuration with talhelper')]
[script]
gen-config:
    just log info "Generating Talos configuration..." "stage" "$0"
    talhelper genconfig --config-file "{{ source_directory() }}/talconfig.yaml" --secret-file "{{ source_directory() }}/talsecret.sops.yaml" --out-dir "{{ source_directory() }}/clusterconfig"

[doc('Generate secret if missing')]
[script]
gen-secret:
    if [ ! -f "{{ source_directory() }}/talsecret.sops.yaml" ]; then
        just log info "Generating Talos secret..." "stage" "$0"
        talhelper gensecret | sops --filename-override talos/talsecret.sops.yaml --encrypt - > "{{ source_directory() }}/talsecret.sops.yaml"
    else
        just log info "Talos secret already exists" "stage" "$0"
    fi

[doc('Apply Talos config to a specific node')]
[script]
apply-node node *args:
    just log info "Applying Talos config to node..." "stage" "$0" "node" "{{ node }}"
    talhelper gencommand apply --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --node {{ node }} --extra-flags "{{ args }}" | bash

[confirm('Upgrade Talos on node? [y|N]')]
[doc('Upgrade Talos on a specific node')]
[script]
upgrade-node node:
    just log info "Upgrading Talos on node..." "stage" "$0" "node" "{{ node }}"
    TALOS_IMAGE=$(yq '.nodes[] | select(.ipAddress == "{{ node }}") | .talosImageURL' "{{ source_directory() }}/talconfig.yaml")
    TALOS_VERSION=$(yq '.talosVersion' "{{ source_directory() }}/talenv.yaml")
    talhelper gencommand upgrade --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --node {{ node }} --extra-flags "--image=${TALOS_IMAGE}:${TALOS_VERSION} --timeout=10m" | bash

[confirm('Upgrade Kubernetes on the cluster? [y|N]')]
[doc('Upgrade Kubernetes version')]
[script]
upgrade-k8s:
    just log info "Upgrading Kubernetes..." "stage" "$0"
    K8S_VERSION=$(yq '.kubernetesVersion' "{{ source_directory() }}/talenv.yaml")
    talhelper gencommand upgrade-k8s --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --extra-flags "--to ${K8S_VERSION}" | bash

[doc('Reset cluster nodes to maintenance mode')]
[script]
reset:
    gum confirm "This will destroy your cluster and reset the nodes back to maintenance mode. Continue?" --default=false || exit 0
    just log warn "Resetting cluster..." "stage" "$0"
    talhelper gencommand reset --config-file "{{ source_directory() }}/talconfig.yaml" --out-dir "{{ source_directory() }}/clusterconfig" --extra-flags "--reboot --system-labels-to-wipe STATE --system-labels-to-wipe EPHEMERAL --graceful=false --wait=false" | bash
