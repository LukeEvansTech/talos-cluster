#!/usr/bin/env bash
# Client-only Talos creds for cloud sessions: the cloud env's own gen-config path 401s on the
# sandbox's placeholder GITHUB_TOKEN. Interim until claude-cloud-env's own hook covers this.
# See docs/docs/troubleshooting/kb/206-claude-cloud-session-talos-cred-fallback.md.
set -uo pipefail

# Never abort session startup. Every failure below is logged and swallowed:
# an unverifiable session is a nuisance, a session that will not start is not.
trap 'exit 0' EXIT

log() { printf 'Talos creds: %s\n' "$1"; }

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT" || exit 0

# Web sessions only: a workstation already has its own talosconfig, and minting a second
# credential set there would be a surprise.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
[ -f talos/talconfig.yaml ] || exit 0

KUBECONFIG_PATH="$ROOT/kubeconfig"
TALOSCONFIG_PATH="$ROOT/talos/clusterconfig/talosconfig"

# mise sets these only for processes it spawns, so export them here for the agent's own shells.
# The tailnet proxy vars stay out: exporting HTTPS_PROXY globally would hijack ordinary outbound
# HTTPS, so callers prefix it per command instead.
# Appended once: this hook also fires on resume and compact.
if [ -n "${CLAUDE_ENV_FILE:-}" ] &&
    ! grep -qF "export KUBECONFIG=\"$KUBECONFIG_PATH\"" "$CLAUDE_ENV_FILE" 2>/dev/null; then
    {
        echo "export KUBECONFIG=\"$KUBECONFIG_PATH\""
        echo "export TALOSCONFIG=\"$TALOSCONFIG_PATH\""
    } >>"$CLAUDE_ENV_FILE"
fi

# A kubeconfig already on disk, from either path, means nothing to do; keeps resume/compact cheap.
if [ -s "$KUBECONFIG_PATH" ]; then
    log "kubeconfig already present, nothing to do."
    exit 0
fi

[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] || {
    log "no OP_SERVICE_ACCOUNT_TOKEN, skipping (cluster access unavailable this session)."
    exit 0
}

command -v op >/dev/null 2>&1 || {
    log "op CLI not found, skipping."
    exit 0
}

# Prefer whatever mise already resolved, else install just this one tool. GITHUB_TOKEN/GH_TOKEN
# are cleared: mise's own installs hit the same sandbox-token 401 as gen-config otherwise. Cleared,
# aqua falls back to anonymous GitHub access, and mise.lock still enforces checksum integrity.
TALOSCTL="$(command -v talosctl 2>/dev/null)"
if [ -z "$TALOSCTL" ]; then
    TALOSCTL="$(GITHUB_TOKEN='' GH_TOKEN='' mise which talosctl 2>/dev/null)"
fi
if [ -z "$TALOSCTL" ] || [ ! -x "$TALOSCTL" ]; then
    GITHUB_TOKEN='' GH_TOKEN='' mise install 'aqua:siderolabs/talos' >/dev/null 2>&1
    TALOSCTL="$(GITHUB_TOKEN='' GH_TOKEN='' mise which talosctl 2>/dev/null)"
fi
if [ -z "$TALOSCTL" ] || [ ! -x "$TALOSCTL" ]; then
    log "talosctl unavailable (install failed), skipping."
    exit 0
fi

# First control-plane IP, same selection the primary path makes with yq. python3 avoids depending
# on another mise-resolved tool; the awk fallback covers a python without PyYAML. Logging this IP
# is fine: talconfig.yaml commits it already.
CP_NODE="$(python3 -c '
import sys, yaml
try:
    d = yaml.safe_load(open("talos/talconfig.yaml"))
    print(next(n["ipAddress"] for n in d["nodes"] if n.get("controlPlane")))
except Exception:
    sys.exit(1)
' 2>/dev/null)"
if [ -z "$CP_NODE" ]; then
    CP_NODE="$(awk '/ipAddress:/ { gsub(/[",]/, "", $2); ip = $2 }
                    /controlPlane:[[:space:]]*true/ { print ip; exit }' talos/talconfig.yaml 2>/dev/null)"
fi
[ -n "$CP_NODE" ] || {
    log "could not determine a control-plane IP from talos/talconfig.yaml, skipping."
    exit 0
}

# Secrets go straight from op into files, never stdout, a variable, or the process table: this
# script's own output is echoed into the session transcript.
umask 077
mkdir -p "$(dirname "$TALOSCONFIG_PATH")" || exit 0
TMP="$(mktemp -d)" || exit 0
# shellcheck disable=SC2064 # expand TMP now: it must be removed even on early exit.
trap "rm -rf '$TMP'; exit 0" EXIT

for field in TALOS_CA TALOS_CRT TALOS_KEY; do
    if ! op read "op://Talos/talos/${field}" >"$TMP/${field}" 2>/dev/null; then
        log "could not read ${field} from 1Password, skipping (check the service-account vault scope)."
        exit 0
    fi
    [ -s "$TMP/${field}" ] || {
        log "${field} came back empty, skipping."
        exit 0
    }
done

# Every control-plane node, so one being down or cordoned doesn't cost the session cluster access.
python3 - "$TMP" "$TALOSCONFIG_PATH" "$CP_NODE" <<'PY' || exit 0
import os, sys, yaml
tmp, out, cp_node = sys.argv[1], sys.argv[2], sys.argv[3]
read = lambda n: open(os.path.join(tmp, n)).read().strip()
try:
    nodes = yaml.safe_load(open("talos/talconfig.yaml"))["nodes"]
    eps = [n["ipAddress"] for n in nodes if n.get("controlPlane")]
except Exception:
    eps = []
# Falls back to the single node resolved above (possibly by awk, if PyYAML is
# what failed) rather than writing a config with an empty endpoint list.
if not eps:
    eps = [cp_node]
cfg = {"context": "talos", "contexts": {"talos": {
    "endpoints": eps,
    "ca": read("TALOS_CA"), "crt": read("TALOS_CRT"), "key": read("TALOS_KEY"),
}}}
with open(out, "w") as f:
    yaml.safe_dump(cfg, f)
PY
chmod 600 "$TALOSCONFIG_PATH" 2>/dev/null

# talosctl's gRPC client honours only HTTPS_PROXY (ALL_PROXY/SOCKS time out). no_proxy/NO_PROXY
# must also be cleared: the sandbox lists RFC1918 and CGNAT there, which would dial direct (no
# route) for the addresses that need the tailnet.
if no_proxy='' NO_PROXY='' http_proxy='' https_proxy=http://localhost:1055 \
    HTTPS_PROXY=http://localhost:1055 \
    "$TALOSCTL" --talosconfig "$TALOSCONFIG_PATH" kubeconfig "$KUBECONFIG_PATH" \
    --nodes "$CP_NODE" --force >/dev/null 2>&1; then
    chmod 600 "$KUBECONFIG_PATH" 2>/dev/null
    log "client talosconfig built from 1Password and kubeconfig fetched from ${CP_NODE}."
    log "read-only client config; cluster-mutating flows still need 'just talos gen-config'."
    log "prefix cluster commands with: no_proxy='' NO_PROXY='' http_proxy='' https_proxy=http://localhost:1055 HTTPS_PROXY=http://localhost:1055"
else
    log "talosconfig built, but kubeconfig fetch from ${CP_NODE} FAILED."
    log "retry: no_proxy='' NO_PROXY='' https_proxy=http://localhost:1055 HTTPS_PROXY=http://localhost:1055 talosctl kubeconfig '${KUBECONFIG_PATH}' --nodes ${CP_NODE} --force"
fi

exit 0
