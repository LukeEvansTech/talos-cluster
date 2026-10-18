# Retiring an app

Archiving an app (the `git mv` to `.archive/` in `AGENTS.md`) removes only what Flux owns. The app
usually also has resources in **other** apps' directories, credentials minted for it elsewhere,
1Password items, and backups outside the cluster. This page is the whole procedure, in order, with
a check at the end that can say no.

Throughout, `<app>` is the app name and `<ns>` its namespace.

!!! warning "The backups are the only undo"

    Once the PVC is pruned, the VolSync backups (NAS Kopia and R2 restic) are the only copy of the
    app's data. Deleting them is step 7, it is optional, and it happens only when someone has asked
    for it. A retirement is complete without it. When in doubt, keep them: their storage cost is
    small, and restic on R2 prunes itself under its retention policy.

## 1. Inventory before the move

Find everything that names the app before anything is removed, while the live objects still
exist to compare against.

```bash
app=<app>; ns=<ns>
# Repository references, whole word. Read every hit: each is either inside the app's own
# directory (moves with it) or something to remove in step 2.
git grep -nwE "$app" -- kubernetes docs
# What Flux applied for it, and what VolSync and the gateway hold.
flux get kustomizations -A | grep -E "(^|[/ ])${app}([- ]|$)"
kubectl get pvc,replicationsources,replicationdestinations,httproutes -n "$ns" | grep -E "(^|[/ ])${app}([- ]|$)"
kubectl get litellmvirtualkeys,pushsecrets -n ai | grep -E "(^|[/ ])${app}([- ]|$)"
```

The pattern also matches `<app>-…` names, because the app's own objects are called `<app>-nfs`,
`<app>-r2` and so on. A line for a different live app that shares the prefix is not a leftover.

Note the answers to three questions for the PR body: what the PVC holds, whether the app has
VolSync backups, and whether anything else depends on it.

**If the data matters, prove a copy exists before step 2 merges**, because the merge deletes the
PVC. For a VolSync app, run `just kube snapshot` (or wait for the schedule) and confirm both
ReplicationSources report a sync after the app's last write:

```bash
kubectl get replicationsources -n "$ns" "$app-nfs" "$app-r2" \
  -o custom-columns=NAME:.metadata.name,LAST:.status.lastSyncTime,RESULT:.status.latestMoverStatus.result
```

An app without the `volsync` component has no backup at all: copy the data out with
`just kube browse-pvc <ns> <claim>` first. Put the evidence (the sync times, or where the copy
went) in the PR body.

## 2. Archive in Git, in one PR

- `git mv kubernetes/apps/<ns>/<app> .archive/kubernetes/apps/<ns>/<app>` and delete its
  `./<app>/ks.yaml` line from `kubernetes/apps/<ns>/kustomization.yaml`. Flux then prunes the
  HelmRelease, the PVC and every other object the app's Kustomization applied.
- **LiteLLM virtual key.** A consumer outside `ai` has
  `kubernetes/apps/ai/litellm/app/virtualkeys/<app>.yaml` (a `LiteLLMVirtualKey` plus its
  `PushSecret`). Delete the file and its entry in that directory's `kustomization.yaml`.
- **Docs tables and counts.** Remove the app's row from every table that lists it and fix any
  count in the prose around it. The [AI / LLM stack](../architecture/ai-llm-stack.md) "In-cluster
  consumers" table and its "N in-cluster apps" sentence are the usual pair. A `docs/docs/apps/<app>.md`
  page either stays in the nav with an "archived" note at the top or is deleted; a page left under
  `docs/docs/` without a nav entry fails the Docs pages linked check.
- **`dependsOn`** naming the app in any other `ks.yaml` or HelmRelease.
- **Homepage tiles** defined outside the app's own `ks.yaml` (a tile from the `homepage` component
  goes with the app).
- **Shared data services.** A CNPG tenant file in `kubernetes/apps/database/cloudnative-pg/tenants/`
  and its `kustomization.yaml` entry. The tenants set `databaseRoleReclaimPolicy: retain`, so the
  role (and the database, under the same policy) stays in Postgres after the file goes; dropping it
  destroys data and belongs with step 7.

This is how `recommendarr` was archived in #6043: the move, the namespace entry, the virtual key
with its PushSecret, and its row in the AI stack docs.

## 3. Run the check

```bash
python3 .github/scripts/check_archived_app_refs.py
```

For every directory under `.archive/kubernetes/apps/<ns>/<app>` with a `ks.yaml`, it takes the
directory name and every Kustomization that `ks.yaml` declares, and fails if a file under
`kubernetes/` still references one of them as a resource: a path component named for it
(`virtualkeys/<app>.yaml`), a kustomization `resources:` or `components:` entry, a `dependsOn`
name, or a `metadata.name`. Matching is whole-token. It runs in lefthook `pre-commit` and as the
**Archived App References** job in `security-scans`. Docs are not in its scope; the `git grep` in
step 1 covers them.

A hit that must stay goes in the script's `ALLOWLIST` with a reason. An entry that stops matching
fails the run, so it cannot linger.

## 4. Confirm Flux pruned it

After the merge reconciles (`flux reconcile source git flux-system`), re-run the step 1 `flux` and
`kubectl` queries; they should print nothing. Then look for objects Flux never owned:

- **Orphaned PVCs.** A chart or StatefulSet can create claims outside the Flux inventory (a
  `volumeClaimTemplates` PVC, a bundled database's PVC), and those survive the prune. List the
  namespace's PVCs and match them to the app by name and labels. With the `ceph-block` reclaim
  policy `Delete`, `kubectl delete pvc` also removes the RBD image. This deletes data: check what
  it holds first (`rbd du` through the `rook-ceph-tools` pod shows real use).
- **DNS.** external-dns removes the app's records once its route is gone; confirm the hostname no
  longer resolves.

## 5. Revoke credentials minted outside the cluster

The app's Kubernetes Secrets are pruned, but the credentials behind them still work wherever they
were issued.

- **Credentials minted in other apps for this one.** An API key created for it in Jellyfin, an
  OAuth client, a token issued to it by another service: revoke each in the service that issued
  it. The app's old `externalsecret.yaml` under `.archive/` lists which 1Password fields it
  consumed, which is the best map of what it held.
- **Shared credentials are not the app's to revoke.** Many of those fields are a service's single
  API key (an `*arr` key, for one) that live apps read too. Before revoking anything, find the
  field's other readers with `git grep -n "<FIELD_NAME>" -- kubernetes`. If any remain, either
  leave it, or rotate it in the issuing service, in 1Password and in every remaining consumer
  together.
- **LiteLLM.** Deleting the `LiteLLMVirtualKey` CR should retire the key; confirm the key alias no
  longer appears in LiteLLM's key list.

## 6. Clean up 1Password

- **The `LITELLM_<APP>_API_KEY` field on the `litellm` item.** The PushSecret wrote it and does not
  remove it when deleted. Delete the field.
- Fields for the app on shared items that are only access credentials (a Garage access key, once
  the key itself is revoked).
- **Keep the app's own item in the `Talos` vault while any of its data is kept.** It may hold what
  the retained data needs to be read: an encryption key (Metabase's `MB_ENCRYPTION_SECRET_KEY`
  decrypts its saved connections), a database password, a restored app's admin login. Delete it
  in step 7, together with the last of the data.

## 7. Delete the backups (only when asked)

This is the irreversible step. Do it only on an explicit request, after the PR has merged and the
app has been gone long enough that nobody wants it back.

### The app's 1Password item

Once nothing of the app's data is kept, confirm no live ExternalSecret extracts the item
(`git grep -nE "key: <item>$" -- kubernetes` prints nothing), then delete it. A deleted item stays
in the vault's Recently Deleted for 30 days.

### R2 (restic)

Each app has one restic repository at `<REPOSITORY_TEMPLATE>/<app>`, the template coming from the
`volsync-r2-template` item in the `Talos` vault. Drive `rclone` from environment variables so no
credential lands in a config file or on screen:

This block may run in a fresh shell long after step 1, so it sets `app` again and refuses to purge
unless the name is a plain app name: an empty `app` would make the target the bucket root, every
app's backups.

```bash
app=<app>
umask 077; d=$(mktemp -d)
for f in REPOSITORY_TEMPLATE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
  op read "op://Talos/volsync-r2-template/$f" > "$d/$f"
done
rest=$(cat "$d/REPOSITORY_TEMPLATE"); rest=${rest#s3:}   # https://<endpoint>/<bucket>
export RCLONE_CONFIG_R2_TYPE=s3 RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ENDPOINT="${rest%/*}"
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$(cat "$d/AWS_ACCESS_KEY_ID")"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$(cat "$d/AWS_SECRET_ACCESS_KEY")"
bucket=${rest##*/}
rclone lsd "r2:$bucket"                 # one directory per app
rclone size "r2:$bucket/$app"           # what is about to go
[[ "$app" =~ ^[a-z0-9][a-z0-9-]*$ && -n "$bucket" ]] && rclone purge "r2:$bucket/$app"
rclone lsd "r2:$bucket" | grep -cE " ${app}$"   # 0
rm -rf "$d"
```

A `GetBucketVersioning` 403 line from `rclone` is noise. To find orphaned prefixes, compare
`rclone lsd` with the live `*-r2` ReplicationSources; a prefix with no source is not necessarily an
archived app, since a live app can drop the `volsync` component and keep its old prefix.

### NAS (Kopia)

Every app shares one Kopia repository, separated only by its source identity,
**`<app>-nfs@<ns>:/data`**. The `:/data` is required: `kopia snapshot list <app>-nfs@<ns>`
returns nothing, silently, and reads like an empty source.

Use the Job from the [Plex media bundles volume](../migrations/plex-media-bundles-volume.md)
runbook (section 11c), with these changes:

- name it `kopia-maint-<app>-purge`, keeping the `kopia-maint-` prefix, which the
  `kopia-maintenance` MutatingAdmissionPolicy keys on to mount the repository;
- `SOURCE` is `<app>-nfs@<ns>:/data`, and the `--override-username` is distinct (for example
  `<app>-purge`), so the Job never becomes the maintenance owner;
- drop the `CUTOFF` selection and select every snapshot of the source:

```bash
IDS=$(kopia snapshot list "$SOURCE" --all --show-identical --manifest-id \
  | awk '{ for (i = 1; i <= NF; i++)
             if ($i ~ /^manifest:/) { sub(/^manifest:/, "", $i); print $i } }')
```

Run it once with `CONFIRM` empty (a dry run: every `Would delete` line must name
`<app>-nfs@<ns>:/data`, and nothing else), then with `CONFIRM=--delete`, then once more dry: the
listing must be empty. Select by manifest ID only, never by object ID, and never use
`--all-snapshots-for-source` or query the Kopia server pod. Space comes back through the hourly
maintenance over 24 to 48 hours. Delete the Job when done.

### Other data

- **NFS side data** the app wrote outside VolSync (dated copies, exports): delete it from a Job
  that mounts the app's own child export as the app's UID and removes the contents with
  `find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +`. The base export is read-only.
- **Postgres** role and database retained from the tenant file (step 2), Garage buckets, Dragonfly
  keys.

## Done when

Every command below prints nothing, apart from lines for a different live app whose name shares
the prefix, and the check passes:

```bash
app=<app>; ns=<ns>; m="(^|[/ ])${app}([- ]|$)"
python3 .github/scripts/check_archived_app_refs.py          # exits 0
flux get kustomizations -A | grep -E "$m"
flux get helmreleases -A | grep -E "$m"
kubectl get pvc,replicationsources,replicationdestinations,httproutes,externalsecrets -A | grep -E "$m"
kubectl get litellmvirtualkeys,pushsecrets -n ai | grep -E "$m"
```

Plus, by hand: the credentials from step 5 are revoked and the 1Password fields from step 6 are
gone. Step 7 is complete only if it was asked for; say in the PR or the issue which backups, and
which 1Password item, were kept.
