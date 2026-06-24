#!/bin/sh
# Per-replica Ollama model reconciler. Runs as a GPU-less sidecar container in
# every Ollama pod (the ollama image used purely as a CLI client against the
# server in the same pod) and keeps the declared model set present on this
# replica's RWO /models PVC. Each replica self-provisions, so the set is
# identical across all replicas by construction.
#
# IMPORTANT: this script must NEVER exit. A crash-looping sidecar would flip the
# pod to NotReady and pull the Ollama server out of the Service. Every fallible
# command is guarded; the wait + reconcile loops run forever.
#
# Flux ${VAR} substitution is DISABLED for this ConfigMap (see
# app/kustomization.yaml: kustomize.toolkit.fluxcd.io/substitute: disabled), so
# the shell vars below are passed through literally.
set -u

CONFIG_DIR="/config"
INTERVAL="${RECONCILE_INTERVAL:-1800}"

log() { echo "[reconcile] $*"; }

# True if $1 matches a model name exactly in `ollama list` (first column).
have_model() {
    ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$1"
}

# Wait for the local Ollama server (same pod) before touching anything.
until ollama list >/dev/null 2>&1; do
    log "waiting for ollama server at ${OLLAMA_HOST:-127.0.0.1:11434} ..."
    sleep 5
done
log "ollama server is up"

while true; do
    # 1. Library pulls (models.list).
    if [ -f "${CONFIG_DIR}/models.list" ]; then
        while IFS= read -r ref; do
            case "${ref}" in '' | \#*) continue ;; esac
            if have_model "${ref}"; then
                log "present: ${ref}"
            else
                log "pulling: ${ref}"
                ollama pull "${ref}" || log "WARN pull failed: ${ref}"
            fi
        done <"${CONFIG_DIR}/models.list"
    fi

    # 2. Modelfile (re)creates (<name>.Modelfile -> model <name>:latest).
    # Always run create so PARAMETER changes (e.g. num_ctx) converge on existing
    # replicas — `ollama create` is cheap when the FROM blob is already local (it
    # just rewrites the manifest) and does not unload a running model. A changed
    # parameter takes effect on the next model load (keep-alive expiry, eviction,
    # or a manual `ollama stop`).
    for mf in "${CONFIG_DIR}"/*.Modelfile; do
        [ -e "${mf}" ] || continue
        name="$(basename "${mf}" .Modelfile)"
        log "ensuring: ${name}"
        ollama create "${name}" -f "${mf}" || log "WARN create failed: ${name}"
    done

    log "reconcile pass complete; sleeping ${INTERVAL}s"
    sleep "${INTERVAL}"
done
