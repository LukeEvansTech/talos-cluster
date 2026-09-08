# Forgejo

GitHub backup target in the `default` namespace. Forgejo (`forgejo.${SECRET_DOMAIN}`,
internal only) holds pull mirrors of every repository worth keeping a copy of; a nightly
`gickup` CronJob keeps the list of mirrors in step with GitHub. Nothing is pushed to
Forgejo directly and nothing clones from it: it is a backup, not a second forge.

## What is mirrored

- Repositories owned by the personal account, private ones included.
- Repositories in the `codelooks-com` organisation.
- Every starred repository (a few hundred, around 25 GB of git data).
- The wiki of each of the above, where one exists.

Forks are skipped. Each upstream owner becomes a private Forgejo organisation, so the
layout matches GitHub (`owner/repo`) and two starred repositories with the same name
cannot collide.

## What is deliberately not mirrored

- **Issues, pull requests, releases and other metadata.** gickup can only dump issues to a
  local directory, and the decision was that git data plus wiki is the backup.
- **Private repositories other people share with the account.** The token lists
  everything it can reach, which includes a paid theme organisation, collaborators'
  private repositories and GitHub preview organisations. gickup's `includeorgs` keeps
  only the two owners above. To add another organisation the account belongs to, add it
  to that list in `configmap.yaml`.
- **Gists and LFS objects.**

## How it works

1. **Forgejo** runs from the forgejo-helm chart with sqlite on a VolSync-backed
   `ceph-block` PVC. It is hardened for its role: `DISABLE_SSH` (nothing clones over
   SSH and the service is ClusterIP-only), `DISABLE_REGISTRATION`, and
   `REQUIRE_SIGNIN_VIEW`. `[git.timeout] MIGRATE` is raised to an hour because the
   largest starred repository is around 4 GB and the 600 s default cannot clone it.
2. **gickup** (`docker.io/buddyspencer/gickup`) runs as an app-template CronJob at 02:00.
   It lists GitHub twice: once with the classic PAT from the `gickup` 1Password item for
   the private set, once unauthenticated for the starred set so that Forgejo stores no
   credential on the public mirrors. For each repository it asks Forgejo to create a pull
   mirror (the synchronous `/repos/migrate` endpoint) or, if one exists, to sync it.
   Forgejo then fetches on its own 24 h timer, so a gickup run is API calls only.
3. **The Forgejo token is minted per run.** Forgejo shows an access token exactly once,
   and only the basic-auth token endpoints can create one, so nothing durable holds it.
   The `token` init container runs `mint_token.py` from the ConfigMap with the admin
   credentials already in `forgejo-secret`: delete any token named `gickup`, create a
   fresh one scoped to `read:misc,read:user,write:organization,write:repository`, write
   it to an emptyDir the gickup container reads through `token_file`. The `read:misc`
   scope covers the `/version` probe the Gitea SDK makes on connect, which
   `REQUIRE_SIGNIN_VIEW` would otherwise reject.
4. **Alerting** follows the repository's absence-of-success pattern:
   `GickupBackupStale` fires when the CronJob has not succeeded for about 26 h.

## Runbook

Trigger a run outside the schedule and follow it:

```bash
kubectl -n default create job --from=cronjob/gickup gickup-manual
kubectl -n default logs -f job/gickup-manual -c token
kubectl -n default logs -f job/gickup-manual -c app
```

Confirm repositories landed (admin credentials from the `forgejo` 1Password item):

```bash
kubectl -n default port-forward svc/forgejo-http 3000:3000
curl -su "$USER:$PASS" 'http://localhost:3000/api/v1/repos/search?limit=1' | jq .total_count
```

## Gotchas

- **One failed repository fails the whole run.** gickup logs each migrate error, deletes
  the half-created repository, carries on, and exits non-zero at the end, so the Job
  fails, retries once (`backoffLimit: 1`, every existing mirror just gets a sync request)
  and the stale alert fires if the night ends without a success. Read the `app` container
  log for the `ERR` lines; the usual cause is upstream, not Forgejo.
- **Forgejo needs memory for the clones, not the web process.** Migrations run `git`
  inside the Forgejo container, and the first pass OOM-killed it at the original 256Mi on
  a small repository, which turned into hundreds of connection-refused errors in gickup's
  log. The limit is 2Gi for that reason; the steady state is about 110Mi.
- **The first run is long.** Forgejo's migrate endpoint clones inline, so registering
  several hundred repositories takes hours; `activeDeadlineSeconds` allows six. Later runs
  are minutes, because existing mirrors only receive a sync request.
- **Organisation names come from GitHub owner logins.** An owner whose login matches a
  Forgejo reserved name cannot be created and that repository is skipped every run.
- **Unauthenticated GitHub calls share the cluster's egress address.** The starred entry
  uses about ten requests against a limit of sixty per hour; another workload burning
  that limit at 02:00 fails the starred pass for the night.
- **Mirrors of public repositories now flow into both VolSync targets.** The Forgejo PVC
  is backed up to NFS and to R2 like every other app; the starred set adds tens of GB to
  each. Trim the star list, or exclude names in `conf.yml`, if that matters.
