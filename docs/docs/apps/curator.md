# Curator

The review page for the weekly library cleanup, in the `custom` namespace, at
`https://curator.${SECRET_DOMAIN}/` on the internal gateway. The cleanup itself still runs as four
CronJobs: [Curatorfilm](curatorfilm.md) and [Curatortv](curatortv.md) plan,
[Curatorfilmjudge](curatorfilmjudge.md) and [Curatortvjudge](curatortvjudge.md) judge, act and
send the weekly push. Each judge's `report` step now posts its signed review document here, and
the push opens this page, where the week's verdicts can be overruled before the next run.

The app is its own repository, `LukeEvansTech/curator` (FastAPI, SQLite, one pod); its README lists
the pages and every setting. The cluster runs the image of its latest release tag, pinned by digest
in `kubernetes/apps/custom/curator/app/helmrelease.yaml`.

## What it does

- **Receives** `POST /publish/film` and `POST /publish/tv` from the two judges. A document is taken
  only with a valid `X-Curator-Signature`, an HMAC keyed with that library's Radarr or Sonarr API
  key, which each judge already holds; no other token exists for publishing. Every accepted
  document is kept as received, and a refused one is listed on the Ops page.
- **Shows** what goes at the next run, what needs a person, what is being kept, what went last
  run, and every past run with the judge's reasons, tokens and cost. Every title carries its
  poster and a link to its IMDb page. The app fetches a poster from Radarr or Sonarr the first
  time the title is shown and keeps it under `posters/` on its volume (about 20 KB each), so the
  browser still loads nothing from outside the app and a deleted title keeps its picture.
- **Asks one title at a time** on `/review`, opened from the Review pill on This week: Keep, let
  it go, or decide later, and the next title is drawn. Keep sets the same tag as This week's
  button; the other two answers change no tag and are kept in the app's own database.
- **Writes** only two tags, `cleanup-keep` and `cleanup-eligible`, through the Radarr and Sonarr
  APIs, when a button is pressed. The next weekly plan reads them; the contract is in
  [Curatorfilm](curatorfilm.md). It never deletes, never sets `keep`, and never touches the
  engines' volumes.
- **Reads** the allow list from both services every six hours: every protected title with the
  reason (`keep`, `keep-review`, `cleanup-keep`, or the rating bar). Only `cleanup-keep` is the
  app's to add or remove. The Ops page says whether the live tags could be read.

## Configuration

| Setting                               | Value                                                                       |
| ------------------------------------- | --------------------------------------------------------------------------- |
| `RADARR_API_KEY`, `SONARR_API_KEY`    | ExternalSecret `curator` from the `radarr` and `sonarr` items, as the jobs  |
| `RADARR_URL`, `SONARR_URL`            | The in-cluster Services in `media`                                          |
| `RADARR_LINK_URL`, `SONARR_LINK_URL`  | `https://radarr.${SECRET_DOMAIN}`, `https://sonarr.${SECRET_DOMAIN}`        |
| `CURATOR_DATA`                        | `/data`, a 2Gi `ceph-block` volume backed up by the `volsync` component     |
| Judges' `CLEANUP_REVIEW_URL`          | `http://curator.custom.svc.cluster.local:8080`                              |
| Judges' `CLEANUP_REVIEW_LINK`         | `https://curator.${SECRET_DOMAIN}/`                                         |

The Deployment uses `strategy: Recreate` so only one pod ever holds the SQLite database.

## Network policy

The page's buttons write tags, so the app is left out of `allow-same-namespace`
(`kubernetes/components/netpol/allow-same-namespace.yaml`). Three rules cover it instead:

- `allow-cross-namespace-ingress` in `custom` admits the gateway, as for every app here.
- `allow-curator-from-judges` in `custom` admits `curatorfilmjudge` and `curatortvjudge`, the only
  pods that publish to it, and only for `POST /publish/film` or `/publish/tv` on port 8080 (a
  Cilium L7 rule). The judge pod's `judge` step talks to the LLM and holds no arr key, so it must
  not reach the button endpoints, which take no credential of their own.
- `deny-curator-judges-to-gateway` in `custom` stops the two judges reaching the gateways, which
  would otherwise carry them past that path limit. Everything the judges call is an in-cluster
  Service, and `enableDefaultDeny: false` leaves the rest of their egress open.
- `allow-curatorapp-from-custom` in `media` lets the app reach `radarr` and `sonarr`, and nothing
  else in that namespace.

Prometheus and Gatus reach it through the cluster-wide observability rule.

## Alerts

`kubernetes/apps/custom/curator/app/prometheusrule.yaml`:

- `CuratorDocumentStale` fires per library when `curator_last_document_timestamp_seconds` is more
  than 8 days old for 2 hours. The judges run weekly, so this is a missed or refused publish, and
  the judge's log says `REVIEW PAGE NOT UPDATED` with the reason. A library that has never
  published reads 0 and fires too.
- `CuratorMetricsMissing` fires when the metric has been absent for 30 minutes.

`.github/scripts/tests/prometheus/curator.yaml` holds both rules to those shapes.

## Known gaps

- The buttons carry no credential of their own, and the route has no gateway sign-in (as
  sleeper). Anyone who can reach the internal gateway, other than the two judges, can press them. Putting the route behind
  the `pocket-id` component needs a `curator-pocket-id` item in 1Password first.
- The allow-list refresh exposes only `curator_allow_refresh_ok`, which cannot show a scheduler
  that stopped after a good run. An alert on its age needs a last-success timestamp from the app.

## Running a judge by hand

A manual run publishes like a scheduled one and sends a push with the link. Run the engine first,
then its judge, waiting for each:

```bash
kubectl -n custom create job --from=cronjob/curatorfilm curatorfilm-manual-N
kubectl -n custom create job --from=cronjob/curatorfilmjudge curatorfilmjudge-manual-N
```

The judge's `app` container logs the publish outcome; the Runs page and
`curator_last_document_timestamp_seconds{library="film"}` confirm the document arrived.
