#!/usr/bin/env bash
# One node of a hand-driven rolling reboot or Talos upgrade, per docs/docs/operations/talos-upgrades.md
# ("Rolling reboot procedure"). Run roll-guards.sh up first and down after the last node.
#
# Usage: roll-node.sh <node> [full|skip|check] [reboot|upgrade]
#   full    cordon, drain, pin miroir-agent off, detach loops, reboot or upgrade, restore, wait (default)
#   skip    resume a node that is already cordoned, drained and pinned
#   finish  resume a node that has already rebooted: wait Ready, unpin, uncordon, re-gate
#   check   non-disruptive: Ceph and etcd gates plus the node's miroir loop count (one short-lived pod)
#   upgrade installs talconfig's image at talenv's talosVersion instead of a plain reboot
# Last line: ROLL-DONE <node> or ROLL-FAIL <node> <reason>. Takes 8-13 min: run it backgrounded.
set -Eeuo pipefail

node=${1:?usage: roll-node.sh <node> [full|skip|finish|check] [reboot|upgrade]}
phase=${2:-full}
mode=${3:-reboot}
repo=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
log() { echo "$(date -u +%T) [$node] $*"; }
fail() {
    echo "ROLL-FAIL $node $*"
    exit 1
}
tools() { kubectl -n rook-ceph exec deploy/rook-ceph-tools -- "$@"; }
# Runs privileged with hostPID on the node, so it is digest-pinned (Renovate: .renovaterc.json5).
helper_image="docker.io/library/busybox:1.38.0@sha256:fd7dc98638c8e305f4dc34e979f1c0fdfdcaeb0fbf8fcff77ae834b6da3d7e6e"
# Any unhandled failure still ends in the marker a backgrounded caller waits on.
trap 'echo "ROLL-FAIL $node unexpected failure at line $LINENO: $BASH_COMMAND"' ERR

ip=$(kubectl get node "$node" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
[ -n "$ip" ] || fail "no InternalIP for node"
all_ips=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{","}{end}')
all_ips=${all_ips%,}

# Healthy apart from the noout flag roll-guards sets: OSDMAP_FLAGS passes only when noout is the
# sole flag, so a stale pause or norecover still fails. AUTH_INSECURE_* is NOT excluded: since the
# cephx aes256k migration nothing is muted, so any such check is real.
ceph_ok() {
    tools ceph status -f json 2>/dev/null | jq -e '
    ([.health.checks | to_entries[]
        | select(.key != "OSDMAP_FLAGS" or .value.summary.message != "noout flag(s) set")] | length == 0)
    and .osdmap.num_up_osds == .osdmap.num_osds and .osdmap.num_in_osds == .osdmap.num_osds
    and ([.pgmap.pgs_by_state[] | select(.state_name != "active+clean") | .count] | add // 0) == 0' >/dev/null
}
# A mover evicted mid-backup loses that run; tuppr's TalosUpgrade gates on the same predicate.
volsync_idle() {
    kubectl get replicationsources -A -o json | jq -e '[.items[]
        | select(any(.status.conditions[]?; .type == "Synchronizing" and .status != "False"))] | length == 0' >/dev/null
}
noout_set() { tools ceph osd dump -f json | jq -e '.flags_set | index("noout")' >/dev/null; }
# Each address reports only its own member, so every control-plane address is queried.
# A member that answers is not necessarily healthy, so the etcd service health is read too.
etcd_ok() {
    local n
    n=$(kubectl get nodes --no-headers | wc -l | tr -d ' ')
    [ "$(talosctl -n "$all_ips" etcd status 2>/dev/null | awk 'NR > 1' | wc -l | tr -d ' ')" = "$n" ] &&
        [ "$(talosctl -n "$all_ips" service etcd 2>/dev/null | grep -cE '^HEALTH +OK *$')" = "$n" ]
}

# Runs a shell snippet in a privileged pod on the node and prints its log. Polls to completion:
# returning before the pod finishes is how a detach silently did nothing (talos-upgrades.md).
on_node() {
    local pod="roll-$RANDOM" overrides
    overrides=$(jq -cn --arg n "$node" --arg c "$1" --arg img "$helper_image" '{apiVersion: "v1", spec: {nodeName: $n, hostPID: true,
    tolerations: [{operator: "Exists"}], containers: [{name: "c", image: $img,
    securityContext: {privileged: true}, command: ["sh", "-c", $c]}]}}')
    kubectl -n kube-system run "$pod" --restart=Never --image="$helper_image" -q --overrides="$overrides" >/dev/null
    for _ in $(seq 1 60); do
        case "$(kubectl -n kube-system get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null)" in
        Succeeded | Failed) break ;;
        esac
        sleep 2
    done
    kubectl -n kube-system logs "$pod" 2>/dev/null || true
    kubectl -n kube-system delete pod "$pod" --wait=false >/dev/null 2>&1 || true
}
# Only miroir's .img-backed loops matter; loop0-5 are Talos's own squashfs images. backing_file is
# rendered relative to the creating mount namespace, so match the suffix, never a /var/ prefix.
# shellcheck disable=SC2016 # expanded by the pod's shell
count_loops='n=0; for b in /sys/block/loop*/loop/backing_file; do [ -f "$b" ] || continue; case $(cat "$b") in *.img*) n=$((n+1));; esac; done; echo "COUNT=$n"'
# shellcheck disable=SC2016
detach_loops='for b in /sys/block/loop*/loop/backing_file; do [ -f "$b" ] || continue; case $(cat "$b") in *.img*) d=${b#/sys/block/}; d=${d%%/*}; losetup -d "/dev/$d" 2>&1 || echo "FAILED $d";; esac; done; echo DETACHED'
loops() { on_node "$count_loops" | grep -o 'COUNT=[0-9]*' || echo COUNT=unknown; }
# Fails closed: true only when the query itself succeeded and found no agent pod on the node.
miroir_gone() {
    local pods
    pods=$(kubectl -n miroir-system get pods -l app.kubernetes.io/name=miroir-agent --field-selector spec.nodeName="$node" -o name) || return 1
    [ -z "$pods" ]
}

if [ "$phase" = check ]; then
    ok=0
    if ceph_ok; then log "ceph ok"; else log "ceph NOT ok" && ok=1; fi
    if etcd_ok; then log "etcd ok"; else log "etcd NOT ok" && ok=1; fi
    n=$(loops)
    log "miroir loops: $n"
    [ "$n" != COUNT=unknown ] || ok=1
    exit "$ok"
fi
case "$phase/$mode" in
full/reboot | full/upgrade | skip/reboot | skip/upgrade | finish/reboot | finish/upgrade) ;;
*) fail "bad arguments: phase=$phase mode=$mode" ;;
esac

if [ "$mode" = upgrade ]; then
    version=$(yq -r -e '.talosVersion' "$repo/talos/talenv.yaml")
    image_url=$(yq -r -e ".nodes[] | select(.ipAddress == \"$ip\") | .talosImageURL" "$repo/talos/talconfig.yaml")
    image="$image_url:$version"
fi

if [ "$phase" = full ]; then
    log "pre-flight"
    noout_set || fail "Ceph noout is not set: run 'just talos roll-guards up' first"
    ceph_ok || fail "Ceph not clean before starting"
    etcd_ok || fail "etcd not healthy before starting"
    for _ in $(seq 1 120); do
        volsync_idle && break
        sleep 10
    done
    volsync_idle || fail "a VolSync ReplicationSource was still synchronizing after 20 min"
    # The wait can be long; nothing disruptive happens on health read before it.
    noout_set || fail "Ceph noout was cleared during the VolSync wait"
    ceph_ok || fail "Ceph not clean after the VolSync wait"
    etcd_ok || fail "etcd not healthy after the VolSync wait"
    # The primary PDB allows 0 disruptions by design. Relaxed per node, not once: the hourly
    # Kustomization reconcile restores it mid-roll. roll-guards.sh down restores it at the end.
    kubectl -n database patch cluster postgres18 --type merge -p '{"spec":{"enablePDB":false}}' >/dev/null
    log "cordon + drain"
    kubectl cordon "$node" >/dev/null
    kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --force --timeout=900s >/dev/null 2>&1 || fail "drain failed"
    log "drained; pinning miroir-agent off the node"
    kubectl -n miroir-system patch ds miroir-agent --type=merge -p "$(jq -cn --arg n "$node" '{spec: {template: {spec: {affinity: {nodeAffinity: {requiredDuringSchedulingIgnoredDuringExecution: {nodeSelectorTerms: [{matchExpressions: [{key: "kubernetes.io/hostname", operator: "NotIn", values: [$n]}]}]}}}}}}}')" >/dev/null
fi

if [ "$phase" != finish ]; then
    for _ in $(seq 1 60); do
        miroir_gone && break
        sleep 3
    done
    miroir_gone || fail "miroir-agent still on the node, or the query failed (it re-attaches loops within ~30s)"
    log "miroir-agent gone; loops before detach: $(loops)"
    on_node "$detach_loops" | grep -E 'FAILED|DETACHED' | sort | uniq -c | sed "s/^/$(date -u +%T) [$node]   /"
    c1=$(loops)
    sleep 15
    c2=$(loops)
    log "loops after detach: $c1 then $c2"
    [ "$c1" = COUNT=0 ] && [ "$c2" = COUNT=0 ] || fail "loop devices remain ($c1, $c2): a busy EPHEMERAL would stall teardown"

    boot_before=$(talosctl -n "$ip" read /proc/sys/kernel/random/boot_id 2>/dev/null)
    if [ "$mode" = upgrade ]; then
        log "upgrading to $version"
        talosctl -n "$ip" upgrade --image "$image" --wait=false >/dev/null 2>&1 || fail "talosctl upgrade refused"
    else
        log "rebooting"
        talosctl -n "$ip" reboot --wait=false >/dev/null 2>&1 || true
    fi
    boot_after=""
    for _ in $(seq 1 90); do
        boot_after=$(talosctl -n "$ip" read /proc/sys/kernel/random/boot_id 2>/dev/null || true)
        [ -n "$boot_after" ] && [ "$boot_after" != "$boot_before" ] && break
        sleep 10
    done
    [ -n "$boot_after" ] && [ "$boot_after" != "$boot_before" ] || fail "no new boot_id after 15 min"
else
    boot_after=$(talosctl -n "$ip" read /proc/sys/kernel/random/boot_id)
fi
log "new boot_id; waiting for the kubelet to report it, then Ready"
# The Node object keeps its pre-reboot Ready and osImage until the new kubelet reports.
for _ in $(seq 1 60); do
    [ "$(kubectl get node "$node" -o jsonpath='{.status.nodeInfo.bootID}')" = "$boot_after" ] && break
    sleep 10
done
[ "$(kubectl get node "$node" -o jsonpath='{.status.nodeInfo.bootID}')" = "$boot_after" ] || fail "kubelet never reported the new boot"
kubectl wait node "$node" --for=condition=Ready --timeout=600s >/dev/null || fail "node not Ready"
os=$(kubectl get node "$node" -o jsonpath='{.status.nodeInfo.osImage}')
log "Ready on $os $(kubectl get node "$node" -o jsonpath='{.status.nodeInfo.kubeletVersion}')"
# talosctl upgrade exits 0 even when the bootloader reverts, so the version is checked here.
if [ "$mode" = upgrade ] && [[ $os != *"($version)"* ]]; then
    fail "came back on $os, not $version: see 'Upgrade didn't take' in talos-upgrades.md"
fi

if [ -n "$(kubectl -n miroir-system get ds miroir-agent -o jsonpath='{.spec.template.spec.affinity}')" ]; then
    kubectl -n miroir-system patch ds miroir-agent --type=json -p '[{"op":"remove","path":"/spec/template/spec/affinity"}]' >/dev/null
fi
kubectl uncordon "$node" >/dev/null
# Unpinning only starts the agent rollout; the next node must not be drained before it is back.
kubectl -n miroir-system rollout status ds/miroir-agent --timeout=600s >/dev/null || fail "miroir-agent did not become ready on the node"
log "unpinned + uncordoned, miroir-agent ready; waiting for Ceph and etcd"
for _ in $(seq 1 90); do
    ceph_ok && etcd_ok && break
    sleep 10
done
ceph_ok || fail "Ceph did not re-converge in 15 min"
etcd_ok || fail "etcd not healthy"
log "Ceph clean, etcd healthy"
echo "ROLL-DONE $node"
