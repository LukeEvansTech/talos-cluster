set quiet := true
set shell := ['bash', '-euo', 'pipefail', '-c']

talos_dir := justfile_dir() + '/talos'

[private]
default:
    just -l talos

[doc('Generate Talos configuration with talhelper')]
gen-config:
    just log info "Generating Talos configuration..." "stage" "$0"
    talhelper genconfig --config-file "{{ talos_dir }}/talconfig.yaml" --secret-file "{{ talos_dir }}/talsecret.sops.yaml" --out-dir "{{ talos_dir }}/clusterconfig"

[doc('Generate secret if missing')]
gen-secret:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f "{{ talos_dir }}/talsecret.sops.yaml" ]; then \
        just log info "Generating Talos secret..." "stage" "$0"; \
        talhelper gensecret | sops --filename-override talos/talsecret.sops.yaml --encrypt /dev/stdin > "{{ talos_dir }}/talsecret.sops.yaml"; \
    else \
        just log info "Talos secret already exists" "stage" "$0"; \
    fi

[doc('Apply Talos config to a specific node')]
apply-node node *args:
    just log info "Applying Talos config to node..." "stage" "$0" "node" "{{ node }}"
    talhelper gencommand apply --config-file "{{ talos_dir }}/talconfig.yaml" --secret-file "{{ talos_dir }}/talsecret.sops.yaml" --out-dir "{{ talos_dir }}/clusterconfig" --node {{ node }} --extra-flags "{{ args }}" | bash

[doc('Upgrade Talos on a specific node')]
upgrade-node node:
    #!/usr/bin/env bash
    set -euo pipefail
    just log info "Upgrading Talos on node..." "stage" "$0" "node" "{{ node }}"
    TALOS_IMAGE=$(yq '.nodes[] | select(.ipAddress == "{{ node }}") | .talosImageURL' "{{ talos_dir }}/talconfig.yaml")
    TALOS_VERSION=$(yq '.talosVersion' "{{ talos_dir }}/talenv.yaml")
    talhelper gencommand upgrade --config-file "{{ talos_dir }}/talconfig.yaml" --secret-file "{{ talos_dir }}/talsecret.sops.yaml" --out-dir "{{ talos_dir }}/clusterconfig" --node {{ node }} --extra-flags "--image=${TALOS_IMAGE}:${TALOS_VERSION} --timeout=10m" | bash

[doc('Upgrade Kubernetes version')]
upgrade-k8s:
    #!/usr/bin/env bash
    set -euo pipefail
    just log info "Upgrading Kubernetes..." "stage" "$0"
    K8S_VERSION=$(yq '.kubernetesVersion' "{{ talos_dir }}/talenv.yaml")
    talhelper gencommand upgrade-k8s --config-file "{{ talos_dir }}/talconfig.yaml" --secret-file "{{ talos_dir }}/talsecret.sops.yaml" --out-dir "{{ talos_dir }}/clusterconfig" --extra-flags "--to ${K8S_VERSION}" | bash

[doc('Reset cluster nodes to maintenance mode')]
reset:
    #!/usr/bin/env bash
    set -euo pipefail
    gum confirm "This will destroy your cluster and reset the nodes back to maintenance mode. Continue?" --default=false || exit 0
    just log warn "Resetting cluster..." "stage" "$0"
    talhelper gencommand reset --config-file "{{ talos_dir }}/talconfig.yaml" --secret-file "{{ talos_dir }}/talsecret.sops.yaml" --out-dir "{{ talos_dir }}/clusterconfig" --extra-flags "--reboot --system-labels-to-wipe STATE --system-labels-to-wipe EPHEMERAL --graceful=false --wait=false" | bash
