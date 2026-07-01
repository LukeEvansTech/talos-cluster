# changedetection.io

Web-page and feed change monitor in the `default` namespace, internal-only at
`changedetection.${SECRET_DOMAIN}`. Runs the bjw-s app-template with two containers: the
changedetection.io app itself and a browserless Chrome sidecar (`PLAYWRIGHT_DRIVER_URL`) for
JS-heavy pages. Feeds and static pages use the basic HTTP fetcher (`html_requests`) — no browser
involved.

**Watch configuration lives on the PVC datastore (`/datastore/url-watches.json`), not in Git.**
This page is the durable record of how the RSS/Atom watches are built and why — the findings below
were established empirically on v0.55.7 and cost real debugging time.

## API access

- REST API at `/api/v1`, authenticated with the `x-api-key` header. The key is
  `settings.application.api_access_token` in the datastore:

  ```bash
  kubectl -n default exec deploy/changedetection -c app -- \
    python3 -c "import json; print(json.load(open('/datastore/url-watches.json'))['settings']['application']['api_access_token'])"
  ```

- Useful endpoints: `POST /api/v1/watch` (create — accepts most watch fields inline),
  `PUT /api/v1/watch/<uuid>` (partial update), `GET /api/v1/watch/<uuid>?recheck=1` (queue a
  check), and `GET /api/v1/watch/<uuid>/history/latest` — the rendered snapshot, which is the
  ground truth when debugging filters.
- **Global application settings have no REST endpoint** (e.g. RSS reader mode) — those are UI-only
  (`/settings`).
- Notifications: watches with an empty `notification_urls` inherit the global Apprise target
  (Pushover). **Set `notification_muted: true` while tuning a watch** — every filter change
  produces a "change" event and fires a real notification.

## RSS/Atom watch recipe

Verified against `https://opentrackers.org/feed/` (WordPress RSS, fixed 20-item window) and
`https://old.reddit.com/r/OpenSignups/new/.rss` (Atom, fixed 25-item window).

1. Enable **RSS reader mode** globally (UI → Settings → RSS tab). This is the load-bearing step —
   see the findings below for why raw-XML filtering does not work.
2. Create the watch with `fetch_backend: html_requests` and no `include_filters`.
3. Reduce the snapshot to stable lines with an extract-text regex:
   `/(?m)^\s*(?:Title|Link): .+/`. The snapshot becomes exactly two lines per feed item; any new
   or edited post changes those lines and triggers a notification whose diff names the post.
4. Leave the diff-type options (`filter_text_added` / `removed` / `replaced`) at their defaults
   (all `true`) — see the pipeline-ordering finding below.
5. Verify with `GET /api/v1/watch/<uuid>/history/latest`: expect `2 × items` clean lines and
   `last_error: false` on the watch.

## Key findings

- **Never XPath raw RSS on this version.** The feed is parsed through lxml's HTML parser, where
  `<title>` and `<link>` are HTML-special elements (`<link>` is void, `<title>` is head-only). The
  tree comes out mangled — titles vanish, link URL text detaches from its element — and both the
  `xpath:` (elementpath) and `xpath1:` (lxml-native) engines see the same garbage. No XPath
  expression fixes a broken parse.
- **RSS reader mode sidesteps the parser entirely.** It runs the feed through `feedparser` and
  renders each entry as labelled text (`Title:` / `Link:` / `PubDate:` / `Content:` …). Channel
  noise like `lastBuildDate` disappears, and Atom is normalised too (feedparser resolves Atom's
  `<link href=…>` attribute, which raw XPath cannot render as text). The setting is global but
  only affects RSS/Atom-content watches; HTML-page watches are untouched.
- **`extract_text` defines the change surface.** Keeping only `Title:`/`Link:` lines excludes
  every observed noise source: WordPress comment counts, post-body edits, re-dated `pubDate`s,
  image `srcset` churn.
- **Non-default diff-type options silently disable `extract_text`.** Pipeline order is: include
  filters → text conversion → *diff-type filtering (early-returns on "no diff")* → extract-text.
  Setting e.g. `filter_text_removed: false` looks reasonable ("alert on additions only") but means
  the extraction regex never runs on unchanged checks, and snapshots alternate between full text
  and diff fragments. Keep all three at `true` and let the extract regex do the filtering.
- **Fixed-window feeds make "added-only" unnecessary anyway.** A post cannot leave an N-item
  window without a new one entering, so a removal-only change cannot occur.
- **Never enable "unique lines in history" for re-bumping sites.** Opentrackers re-dates an old
  post when a tracker's signup reopens — the title/link lines are identical to a previous
  appearance, so unique-line suppression would hide exactly the events the watch exists for.

## Reddit specifics

- **Reddit 429s changedetection's default (python-requests-style) User-Agent outright.** Set a
  per-watch descriptive UA via the watch `headers` field, e.g.
  `changedetection.io/0.55 (self-hosted; r/OpenSignups feed watch)`. With that, fetches are clean.
- Observed unauthenticated rate limit: roughly one request per 10 seconds per UA+IP
  (`x-ratelimit-*` response headers). One check per minute is comfortably inside it; bursts are
  not — space manual rechecks more than ~12 seconds apart, and remember the cluster and the
  workstation share the same egress IP.
- On a throttle the watch flags an error state and retries at the next interval — failures are
  visible, not silent misses.

## Current watches

| Watch                       | Source                             | Interval | Notes                            |
| --------------------------- | ---------------------------------- | -------- | -------------------------------- |
| Opentrackers.org new posts  | `opentrackers.org/feed/` (RSS)     | 1 h      | Feed itself updates hourly       |
| r/OpenSignups new posts     | `…/r/OpenSignups/new/.rss` (Atom)  | 1 min    | Custom User-Agent required       |
