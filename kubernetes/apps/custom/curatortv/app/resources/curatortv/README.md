# curatortv

The deterministic half of the weekly Sonarr library cleanup, forked from the
film engine in `kubernetes/apps/custom/curatorfilm/app/resources/curator/`. Judgement
belongs to `curatortvjudge`. The film engine's readme is the design document for
everything shared: what the engine decides and in what order, the fail-closed
rules, why a zero play count has four meanings, why the identity join runs from
history rows, why every output names its plan, and why the volatile checks are
re-read per deletion. All of that holds here unchanged. This file covers only
what is different about TV.

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

## Exit codes

`execute` exits non-zero for anything that should not read as a successful week,
which is what the staleness alert keys on. The CronJob's shell keeps the first
non-zero code and still sends the digest.

| Code | Meaning                                                           |
| ---- | ----------------------------------------------------------------- |
| 2    | the plan recorded blocking issues                                 |
| 3    | another run holds the execute lock                                |
| 4    | a deletion failed or could not be confirmed                       |
| 5    | refused: `--read-only` in act mode, or a stale or wrong-mode plan |
| 7    | the recommendations were judged against a different plan          |
| 8    | a rating-bar `keep` tag could not be confirmed on re-read         |
| 9    | no offered candidate got a verdict, or a verdict was rejected     |
| 10   | a deletion was skipped because a safety check could not be read   |

`report` exits 6 when the digest could not be delivered, and 11 when a review
page is configured and did not take the document; with none configured, 12
when the proposals could not be read for the digest.

## The rating bar's `keep` is written in a dry run here

Both engines can add the rating bar's `keep` tag in a dry run when
`TAG_KEEPS_IN_DRY_RUN=true`, even under `--read-only`; only this one's manifest
sets it. Reclaimerr protects anything tagged `keep`, so a dry run that writes
nothing leaves it blind to the rating bar's decisions; and an add-only tag is
reversible by removing it, which a deletion is not.

The live tags are re-read before each addition, and a series carrying
`cleanup-eligible`, `cleanup-dismissed` or `keep-review` is never tagged: those
are a person's decision. Deletion is untouched by the flag and stays impossible
in a dry run or under `--read-only`, exactly as in the film engine. The result
file reports these writes as `keep_tags_added`; each tag is confirmed by
re-reading the series, and one that did not land is tried again next run, and
the run exits non-zero.

## Three tiers of keeping

As in the film engine. Keep for good is a person (`cleanup-keep`), the rating
bar, or an existing `keep` tag: never judged again. Keep for a year is the
judge's keep: once its review window has passed it writes no tag, in any mode,
and becomes a stored decision (`spared`, with `kept_until` set
`CLEANUP_KEEP_DAYS` ahead, 365 by default) that carries the fact fingerprint,
so the series is judged again after that date, or sooner when somebody watches
it or a season arrives. A series a person tags `cleanup-keep` during the review
week closes with no decision; one tagged `cleanup-eligible` is left for that
person's delete. Everything else goes through the weekly cycle. The result file
reports the judge's due keeps as `judge_keeps`.

## What changes for a series

**The unit is the series.** `series_id` replaces `movie_id` everywhere: the plan,
the recommendations, the ledger keys and the judge prompt. Deleting removes the
whole series with its files and adds an import-list exclusion keyed on TVDB ID.

**Availability is per episode.** Each episode keeps its _earliest_ import, so a
quality upgrade cannot restart a clock. An ended series' window runs from its
first import. A continuing series somebody has played runs from the newest
episode's first import, because a season that arrived last month is not
something anybody has had a chance to watch. A continuing series nobody has
played at all runs from its first import: left on the newest episode's clock,
an airing show nobody watches never came due. That zero has to be trustworthy,
so a history that is incomplete, unresolved or unavailable keeps the later
clock. The window is 180 days, 60 when the IMDb score is under 5.5. The
history-coverage check always uses the series' first import: old seasons were
watchable too, and their plays have to be visible.

**Viewing is counted in episodes.** Tautulli episode rows are joined to a series
by `grandparent_rating_key`, the show's own Plex item. Every key sharing any
TVDB, TMDb or IMDb ID with the series counts, since an older key may know it by
only one of them; a key naming a different ID under any scheme is another show.
The title-and-year fallback (used only when no ID matches), retired-key taint and
ambiguity rules are as in the film engine. A person counts as a
viewer once they finish (85%+) one episode; two viewers protect the series,
mirroring two completers for a film. `episodes_completed` is the most
distinct episodes any one person has finished.

**The rating bar does not apply to reality series.** A series whose genres
include `Reality` is neither protected by the bar nor given the `keep` tag by
it, whatever its rating: for these a rating says little about whether anyone
here wants them, so the judge sees them all. One that already carries `keep`
stays protected, and a keep the judge hands down lasts a year like any other.

**A retired show key taints on title alone.** An episode row's `year` is the
episode's, not the premiere's, so a play behind an unresolvable show key taints
every series with that title; only a show year from the row's own show metadata
narrows it. A Plex item that answers with no TVDB, TMDb or IMDb ID taints the
same way, and is asked about again next run rather than cached.

**A person's tag stops a deletion.** The live re-check refuses a series that
gained `cleanup-dismissed`, as well as any veto tag, since the plan.

**New episodes stop a deletion.** Immediately before each delete, the series'
import history and episode-file count are re-read; anything imported after the
plan, or more files than the plan saw, skips the series this week.

**A play stops a deletion that rested on there being none.** A continuing
series that was due only because nobody had played it is skipped if anybody has
played any part of an episode since the plan. An unfinished play is invisible to
the completion check, and it puts the series back on its newest episode's clock.

**Provenance comes from Kometa tags.** Sonarr here has no import lists. A series
is `feed` when it carries any tag in `FEED_TAGS` (default `kometa`), `requested`
when Seerr has a request for its TVDB ID (or TMDb ID as a fallback), and
`unknown` otherwise. As for films, origin is on the judge's record and is not a
permission: none of the three holds a series back or lets it through. A show is
often requested a season at a time, so the earliest request is kept as the
origin and the newest is what "requested since the plan" is tested against.

**There are no collections.** The film engine's monitored-collection protection,
`siblings_owned` and collection anomaly checks have no TV equivalent and are
gone. Genre `subject_counts` remain.

**Scope.** `upcoming` series are out of scope. Everything else is in, unless
`CLEANUP_SCOPE_START` is set: then a series added before that date is out of
scope, as before.

**Budgets.** `CLEANUP_JUDGE_LIMIT` defaults to 30 candidates a run and
`CLEANUP_MAX_DELETIONS` to 10 deletions a run, counted over six days.

**The review window** is the film engine's, keyed `series_id`: a verdict is a
proposal that waits `CLEANUP_GRACE_DAYS` (default 6) before `execute` acts on
it, `cleanup-keep` ends it, and `cleanup-eligible` is a person's delete that is
never sent to the judge. A series' fingerprint has its season count and status
where a film's has its collection, so a new season or a series ending lapses a
proposal and the series is judged again. `report` builds the same review
document (`review.py` is shared) as the `tv` library; when `CLEANUP_REVIEW_URL`
is set it posts it to `/publish/tv` signed with the Sonarr API key, and
otherwise sends nothing and mentions no page. `CLEANUP_ACCEPT_BASELINE` accepts
a blocked run's figures as it does for films. The film engine's readme has the
rules in full, and the review document's contract.

## Configuration

As the film engine, with `SONARR_URL` / `SONARR_API_KEY` in place of the Radarr
pair, plus:

| Variable               | Meaning                                                                    |
| ---------------------- | -------------------------------------------------------------------------- |
| `FEED_TAGS`            | comma-separated tag labels proving a feed added a series                   |
| `TAG_KEEPS_IN_DRY_RUN` | `true` lets a dry run add the rating bar's `keep` tag; deletion unaffected |
| `CLEANUP_KEEP_DAYS`    | days a judge's due keep spares a series before re-judging; default 365     |

## Undoing a deletion

1. Re-add the series in Sonarr from the intent record, which holds `tvdbId`,
   `path`, `rootFolderPath`, `qualityProfileId`, `seriesType`, `seasonFolder`,
   `monitored`, per-season monitoring and the tag IDs.
2. Restore the files from the recycle bin and rescan.
3. Remove the import-list exclusion, found by **tvdbId** under
   `/api/v3/importlistexclusion`.
