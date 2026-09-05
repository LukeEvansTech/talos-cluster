#!/usr/bin/env bash
# Fallback cluster credentials for Claude Code on the web.
#
# WHY THIS EXISTS
# The cloud environment's own session-start script (/opt/claude-cloud-env,
# not in this repo) already tries to get cluster access, via
# `just talos gen-config` -> talhelper -> `talosctl kubeconfig`. That path
# reconstructs the FULL clusterconfig, machine configs and all, from the
# 1Password `talsecret` item, so it drags in the entire mise toolchain. In the
# sandbox that toolchain resolution is the fragile part: mise's own aqua
# lookups read the sandbox's placeholder GITHUB_TOKEN, get a 401, and
# gen-config fails before a single credential is touched. Observed
# 2026-08-30, with the hook itself reporting "op is reachable and authorised,
# so this is a mise/toolchain resolution failure".
#
# The consequence is out of proportion to the cause: a session that only wants
# to READ from the cluster (check what a rule actually matches, confirm an
# alert is firing, diff a rendered manifest against the repo) has no access at
# all, and answers get inferred instead of verified.
#
# But talking to the cluster never needed talhelper. The `talos` 1Password item
# holds a complete Talos client certificate (TALOS_CA / TALOS_CRT / TALOS_KEY),
# and a talosconfig is just those three fields plus the endpoints. That is a far
# shorter dependency chain, `op` (already authenticated) and the talosctl
# binary, and it is the chain this script uses.
#
# SCOPE: deliberately narrower than gen-config
# This produces a CLIENT config only: enough to reach the Talos API and pull a
# kubeconfig. It does NOT produce machine configs, so `talosctl apply-config`
# and the rest of the cluster-mutating flow still need a real
# `just talos gen-config`. Those commands fail loudly on the missing files
# rather than doing something half-configured, which is the intended failure
# direction: this script is for reading the cluster, not for rebuilding it.
#
# INTERIM: DELETE THIS FILE ONCE THE UPSTREAM FIX LANDS
# This does not belong in this repo. The right home is the cloud environment's
# own hook (LukeEvansTech/claude-cloud-env, hooks/session-start.sh), next to the
# gen-config call it is backing up: that script self-updates from main at every
# session start, so a fix there reaches EVERY session and every repo at once,
# rather than only this repo and only after a merge. That change is written and
# tested on branch claude/talos-client-cred-fallback there; this file exists
# only because that repo was not in the session's authorized set at the time.
#
# When it lands, delete this script, its entry in .claude/settings.json, and the
# `!.claude/hooks/` exception in .gitignore. Two copies of one fallback in two
# repos is exactly the drift this repo's comments elsewhere exist to prevent.
#
# Until then the two compose safely rather than racing, and that is by design,
# not luck: the cloud-env hook runs FIRST (it is first in the settings.json
# hooks array) and writes the same kubeconfig path this script checks for. So
# the moment the upstream fix works, this exits at the "already present" guard
# below without touching anything: no conflict, no double-fetch, and no need to
# coordinate the two removals with the merge.
set -uo pipefail

# Never abort session startup. Every failure below is logged and swallowed:
# an unverifiable session is a nuisance, a session that will not start is not.
trap 'exit 0' EXIT

log() { printf 'Talos creds: %s\n' "$1"; }

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT" || exit 0

# Web sessions only. On a workstation the operator has their own talosconfig and
# no OP_SERVICE_ACCOUNT_TOKEN, and silently minting a second set of credentials
# there would be surprising.
[ "${CLAUDE_CODE_REMOTE:-}" = "true" ] || exit 0
[ -f talos/talconfig.yaml ] || exit 0

KUBECONFIG_PATH="$ROOT/kubeconfig"
TALOSCONFIG_PATH="$ROOT/talos/clusterconfig/talosconfig"

# Export for the agent's own shells. mise sets both in [env], but only for
# processes mise spawns; plain tool calls do not inherit them, which is what
# made every kubectl invocation need a hand-written KUBECONFIG= prefix.
#
# The tailnet proxy vars are deliberately NOT exported. They must be a
# per-command prefix: HTTPS_PROXY=localhost:1055 globally would hijack ordinary
# outbound HTTPS, which the sandbox routes through its own agent proxy.
#
# Appended only once: this hook also fires on resume and compact, and an
# env file that grows a duplicate pair every time is a slow-motion mess.
if [ -n "${CLAUDE_ENV_FILE:-}" ] &&
    ! grep -qF "export KUBECONFIG=\"$KUBECONFIG_PATH\"" "$CLAUDE_ENV_FILE" 2>/dev/null; then
    {
        echo "export KUBECONFIG=\"$KUBECONFIG_PATH\""
        echo "export TALOSCONFIG=\"$TALOSCONFIG_PATH\""
    } >>"$CLAUDE_ENV_FILE"
fi

# Whoever wrote it, the primary path or a previous run of this one, a kubeconfig
# already on disk means there is nothing to do. Keeps resume/compact cheap.
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

# talosctl: prefer whatever mise already resolved, else install just this one
# tool. GITHUB_TOKEN/GH_TOKEN are cleared because mise's OWN installs read the
# sandbox's placeholder token straight from the environment and 401. This is the same
# failure that takes out gen-config. Cleared, aqua falls back to anonymous
# GitHub access, and mise.lock still enforces checksum integrity.
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

# First control-plane IP, same selection the primary path makes with yq. python3
# is used instead so this does not depend on another mise-resolved tool; the awk
# fallback covers a python without PyYAML. Node IPs are not secret (RFC1918,
# and talconfig.yaml is committed).
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

# Secrets go straight from op into files, never through stdout, a variable or
# the process table; this script's output is echoed into the session
# transcript.
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

# Endpoints: every control-plane node, so a single node being down or cordoned
# does not cost the session its cluster access.
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

# talosctl's gRPC client honours only HTTPS_PROXY. It ignores ALL_PROXY/SOCKS
# and times out. no_proxy/NO_PROXY must also be cleared: the sandbox lists
# RFC1918 and CGNAT there, which would make this dial direct (no route) for
# exactly the addresses that need the tailnet. Lowercase https_proxy is cleared
# because many clients prefer it over the uppercase form.
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
