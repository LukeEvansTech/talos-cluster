#!/usr/bin/env -S just --justfile

set lazy
set positional-arguments := true
set quiet := true
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']

# Bootstrap Recipes
[group: 'Bootstrap']
mod bootstrap "bootstrap"

# Kube Recipes
[group: 'Kube']
mod kube "kubernetes"

# Talos Recipes
[group: 'Talos']
mod talos "talos"

[private]
default:
    just -l

[private]
log lvl msg *args:
    gum log -t rfc3339 -s -l "{{ lvl }}" "{{ msg }}" {{ args }}

[private]
template file *args:
    minijinja-cli "{{ file }}" {{ args }} | op inject

# Run super-linter locally with the same env flags as the shared CI workflow.
# slim-v8 is amd64-only — `--platform linux/amd64` enables Rosetta emulation
# on Apple Silicon. RUN_LOCAL=true lints the working tree (skips git-diff logic).
lint *args:
    docker run --rm --platform linux/amd64 \
      -e RUN_LOCAL=true \
      -e DEFAULT_BRANCH=main \
      -e VALIDATE_ALL_CODEBASE=true \
      -e VALIDATE_KUBERNETES_KUBECONFORM=false \
      -e VALIDATE_BIOME_FORMAT=false \
      -e VALIDATE_BIOME_LINT=false \
      -e VALIDATE_CHECKOV=false \
      -e VALIDATE_TRIVY=false \
      -e VALIDATE_GITLEAKS=false \
      -e VALIDATE_JSCPD=false \
      -e LOG_LEVEL=NOTICE \
      -v "$PWD":/tmp/lint \
      ghcr.io/super-linter/super-linter:slim-v8 {{ args }}
