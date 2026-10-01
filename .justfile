#!/usr/bin/env -S just --justfile

set default-list
set default-script
set lazy
set positional-arguments := true
set quiet := true
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']

[group: 'Bootstrap']
mod bootstrap "bootstrap"

[group: 'Kube']
mod kube "kubernetes"

[group: 'Talos']
mod talos "talos"

[doc('Re-run claude/renovate-review on PRs it failed on the usage limit (429); probe re-runs one, all re-runs every one')]
renovate-rerun-limited mode="probe":
    "{{ justfile_directory() }}/scripts/renovate-rerun-limited.sh" {{ mode }}

[private]
log lvl msg *args:
    gum log -t rfc3339 -s -l "{{ lvl }}" "{{ msg }}" {{ args }}

[private]
template file *args:
    minijinja-cli "{{ file }}" {{ args }} | vals eval -f -

# Runs super-linter locally with the same env flags as .github/workflows/lint.yml. slim-v8 is
# amd64-only, so --platform linux/amd64 enables Rosetta on Apple Silicon, and RUN_LOCAL=true lints
# the working tree instead of a git diff. Keep FILTER_REGEX_EXCLUDE in sync with lint.yml's
# filter-regex-exclude, or local also lints the docs/ tree CI excludes. VALIDATE_ALL_CODEBASE is
# true here (CI defaults to changed files only) so a local run checks everything.
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
      -e FILTER_REGEX_EXCLUDE='(^|/)(node_modules|\.venv|site)/|(^|/)docs/' \
      -e LOG_LEVEL=NOTICE \
      -v "$PWD":/tmp/lint \
      ghcr.io/super-linter/super-linter:slim-v8 {{ args }}
