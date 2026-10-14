# curatortv

The deterministic half of the weekly Sonarr library cleanup, forked from the
film engine in `kubernetes/apps/media/curator/app/resources/curator/`. Judgement
belongs to `curatortvjudge`. The film engine's readme is the design document for
everything shared: the fail-closed rules, why a zero play count has four
meanings, why the identity join runs from history rows, why every output names
its plan, and why the volatile checks are re-read per deletion. All of that
holds here unchanged. This file covers only what is different about TV.

## Commands

Run from the directory above this one, which is the package root.

```bash
python3 -m curatortv plan --out ./cleanup-plan      # read-only, always safe
python3 -m curatortv execute --plan ./cleanup-plan/plan.json \
    --recommendations ./recommendations.json --out ./result.json --read-only
python3 -m curatortv report --plan ./cleanup-plan/plan.json \
    --result ./result.json --out ./report.md        # read-only, always safe
python3 -m curatortv selftest                       # fixture tests, no network
python3 -m curatortv.provision                      # show decision-tag changes
```

## The one deliberate deviation: `keep` is written in a dry run

The film engine writes nothing at all in a dry run. This one adds the `keep`
tag, to the series over the rating bar and to every series the judge verdicts
`keep`, when `TAG_KEEPS_IN_DRY_RUN=true`, even under `--read-only`. Reclaimerr
protects anything tagged `keep`, so a dry run that writes nothing leaves it
blind to every decision this engine has made; and an add-only tag is reversible
by removing it, which a deletion is not.

The live tags are re-read before each addition, and a series carrying
`cleanup-eligible`, `cleanup-dismissed` or `keep-review` is never tagged: those
are a person's decision. Deletion is untouched by the flag and stays impossible
in a dry run or under `--read-only`, exactly as in the film engine.

A judge `keep` is therefore permanent rather than a 180-day spare: once tagged,
the series is an absolute veto until a person removes the tag. The result file
reports both writes, as `keep_tags_added` (rating bar) and `judge_keeps_tagged`.

## What changes for a series

**The unit is the series.** `series_id` replaces `movie_id` everywhere: the plan,
the recommendations, the ledger keys and the judge prompt. Deleting removes the
whole series with its files and adds an import-list exclusion keyed on TVDB ID.

**Availability is per episode.** Each episode keeps its _earliest_ import, so a
quality upgrade cannot restart a clock. An ended series' window runs from its
first import. A continuing series' window runs from the newest episode's first
import, because a season that arrived last month is not something anybody has
had a chance to watch. The history-coverage check still uses the series' first
import: old seasons were watchable too, and their plays have to be visible.

**Viewing is counted in episodes.** Tautulli episode rows are joined to a series
by `grandparent_rating_key`, the show's own Plex item, and that key is resolved
to TVDB, then TMDb, then IMDb IDs, with the same title-and-year fallback,
retired-key taint and ambiguity rules as the film engine. A person counts as a
viewer once they finish (85%+) one episode; two viewers send the series to a
person, mirroring two completers for a film. `episodes_completed` is the most
distinct episodes any one person has finished.

**Provenance comes from Kometa tags.** Sonarr here has no import lists. A series
is `feed` when it carries any tag in `FEED_TAGS` (default `kometa`), `requested`
when Seerr has a request for its TVDB ID (or TMDb ID as a fallback), and
`unknown` otherwise. Unknown origin is never authorised for deletion; a person's
`cleanup-eligible` tag authorises it.

**There are no collections.** The film engine's monitored-collection protection,
`siblings_owned` and collection anomaly checks have no TV equivalent and are
gone. Genre `subject_counts` remain.

**Scope.** `upcoming` series are out of scope. `CLEANUP_SCOPE_START` gates on the
added date as before; Sonarr was rebuilt in February 2026, so most added dates
fall then, before the default `2026-03-01`.

## Configuration

As the film engine, with `SONARR_URL` / `SONARR_API_KEY` in place of the Radarr
pair, plus:

| Variable               | Meaning                                                       |
| ---------------------- | ------------------------------------------------------------- |
| `FEED_TAGS`            | comma-separated tag labels proving a feed added a series      |
| `TAG_KEEPS_IN_DRY_RUN` | `true` lets a dry run add the `keep` tag; deletion unaffected |

## Undoing a deletion

1. Re-add the series in Sonarr from the intent record, which holds `tvdbId`,
   `path`, `rootFolderPath`, `qualityProfileId`, `seriesType`, `seasonFolder`,
   `monitored`, per-season monitoring and the tag IDs.
2. Restore the files from the recycle bin and rescan.
3. Remove the import-list exclusion, found by **tvdbId** under
   `/api/v3/importlistexclusion`.
