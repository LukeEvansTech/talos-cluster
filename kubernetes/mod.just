set default-list
set default-script
set lazy
set positional-arguments := true
set quiet := true
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']

[doc('Apply local Flux Kustomization')]
apply-ks ns ks:
    just kube render-local-ks "{{ ns }}" "{{ ks }}" | kubectl apply --server-side --field-manager=kustomize-controller --force-conflicts -f -

[doc('Browse a PVC')]
browse-pvc namespace claim:
    kubectl browse-pvc -n {{ namespace }} -i mirror.gcr.io/alpine:latest {{ claim }}

[doc('Delete local Flux Kustomization')]
delete-ks ns ks:
    just kube render-local-ks "{{ ns }}" "{{ ks }}" | kubectl delete -f -

[doc('Render a HelmRelease offline with flate (postBuild substitutions resolved)')]
flate-build-hr ns hr:
    flate build hr "{{ hr }}" --namespace "{{ ns }}" --path "{{ source_directory() }}/flux/cluster" --allow-missing-secrets

[doc('Render a Kustomization offline with flate (postBuild substitutions resolved)')]
flate-build-ks ns ks:
    flate build ks "{{ ks }}" --namespace "{{ ns }}" --path "{{ source_directory() }}/flux/cluster" --allow-missing-secrets

[doc('Test all manifests offline with flate (flux-local test equivalent)')]
flate-test:
    flate test all --path "{{ source_directory() }}/flux/cluster" --allow-missing-secrets

[doc('Open a shell on a node')]
node-shell node:
    kubectl debug node/{{ node }} -n default -it --image="mirror.gcr.io/alpine:latest" --profile sysadmin
    kubectl delete pod -n default -l app.kubernetes.io/managed-by=kubectl-debug

[doc('Prune pods in Failed, Pending, or Succeeded state')]
prune-pods:
    for phase in Failed Pending Succeeded; do
        kubectl delete pods -A --field-selector status.phase="$phase" --ignore-not-found=true
    done

[doc('Snapshot VolSync PVCs')]
snapshot:
    kubectl get replicationsources --no-headers -A | while read -r ns name _; do
        kubectl -n "$ns" patch replicationsources "$name" --type merge -p '{"spec":{"trigger":{"manual":"$(date +%s)"}}}'
    done

[doc('Pin (suspend) or release (resume) zeroscaler HPAs during node drains')]
zeroscaler state:
    kubectl get hpa -A -l app.kubernetes.io/part-of=zeroscaler --no-headers | while read -r ns name _; do
        kubectl -n "$ns" patch hpa "$name" --type merge -p '{"spec":{"minReplicas":{{ if state == "suspend" { "1" } else { "0" } }}}}'
    done

[doc('Suspend or resume VolSync')]
volsync state:
    flux -n volsync-system {{ state }} kustomization volsync
    flux -n volsync-system {{ state }} helmrelease volsync
    kubectl -n volsync-system scale deployment volsync --replicas {{ if state != "suspend" { "1" } else { "0" } }}

[doc('Sync ExternalSecrets')]
sync-es:
    kubectl get es --no-headers -A | while read -r ns name _; do
        kubectl -n "$ns" annotate --field-manager flux-client-side-apply --overwrite es "$name" force-sync="$(date +%s)"
    done

[doc('Sync GitRepositories')]
sync-git:
    kubectl get gitrepo --no-headers -A | while read -r ns name _; do
        kubectl -n "$ns" annotate --field-manager flux-client-side-apply --overwrite gitrepo "$name" reconcile.fluxcd.io/requestedAt="$(date +%s)"
    done

[doc('Sync HelmReleases')]
sync-hr:
    kubectl get hr --no-headers -A | while read -r ns name _; do
        kubectl -n "$ns" annotate --field-manager flux-client-side-apply --overwrite hr "$name" reconcile.fluxcd.io/requestedAt="$(date +%s)" reconcile.fluxcd.io/forceAt="$(date +%s)"
    done

[doc('Sync Kustomizations')]
sync-ks:
    kubectl get ks --no-headers -A | while read -r ns name _; do
        kubectl -n "$ns" annotate --field-manager flux-client-side-apply --overwrite ks "$name" reconcile.fluxcd.io/requestedAt="$(date +%s)"
    done

[doc('Sync OCIRepositories')]
sync-oci:
    kubectl get ocirepo --no-headers -A | while read -r ns name _; do
        kubectl -n "$ns" annotate --field-manager flux-client-side-apply --overwrite ocirepo "$name" reconcile.fluxcd.io/requestedAt="$(date +%s)"
    done

[doc('View a secret')]
view-secret namespace secret:
    kubectl view-secret -n {{ namespace }} {{ secret }}

[private]
render-local-ks ns ks:
    flate build ks "{{ ks }}" --namespace "{{ ns }}" --path "{{ source_directory() }}/flux/cluster" --allow-missing-secrets
