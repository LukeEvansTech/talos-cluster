# Curatortv

The deterministic half of the weekly Sonarr library cleanup, in the `custom` namespace. A fork of
[Curator](curator.md) with TV semantics: it assesses every series and writes `cleanup-plan/plan.json`
on its own volume, and `curatortvjudge` reads that plan half an hour later. The shared design
(fail-closed sources, the identity join, per-deletion re-checks, execute failure semantics) is on
the [Curator](curator.md) page and in the film engine's README; this page covers what differs.

The engine lives in `kubernetes/apps/custom/curatortv/app/resources/curatortv/`, shipped as the
`curatortv-code` ConfigMap. Its `README.md` is the design note for the TV differences.

## What differs from Curator

- **Unit of deletion:** a whole series, keyed `series_id` in the plan, recommendations, ledger and
  judge prompt. The delete adds an import-list exclusion keyed on TVDB id.
- **Availability:** each episode keeps its earliest import. An ended series' window runs from its
  first import. A continuing series somebody has played runs from the newest episode's first
  import, so a new season gets a full window. A continuing series with no plays at all, in a play
  history that can be trusted, runs from its first import: on the newest episode's clock an airing
  show nobody watches never came due.
- **Viewing:** Tautulli episode rows joined by the show's rating key (`grandparent_rating_key`),
  counting every key that shares a TVDB, TMDb or IMDb id and none that conflicts. A viewer is someone who finished at least one episode;
  two viewers protect the series.
- **Reality:** the rating bar neither protects nor tags a series whose genres include `Reality`,
  so the judge sees those whatever their score. One already tagged `keep` stays protected.
- **Provenance:** Kometa's tags (`FEED_TAGS`, default `kometa`) mark a feed add, since Sonarr here
  has no import lists. Seerr requests match on TVDB id, then TMDb. As for films, origin goes on the
  judge's record and is not a permission: a requested series and one of unknown origin are both
  candidates once their window has passed.
- **Late arrivals:** each deletion re-reads the series' import history and episode-file count, and
  skips a series that gained anything since the plan. A continuing series that was due only because
  nobody had played it is also skipped if any part of an episode has been played since the plan,
  finished or not.
- **No collections:** collection protection and `siblings_owned` are gone.
- **Scope:** `upcoming` series are out of scope; nothing else is, since `CLEANUP_SCOPE_START` is
  no longer set.
- **Keep tags in a dry run:** with `TAG_KEEPS_IN_DRY_RUN=true` the executor adds `keep` to
  rating-bar series and to judge `keep` verdicts even in a dry run, so Reclaimerr (which spares
  anything tagged `keep`) sees them. Deletion stays impossible. See
  [Curatortvjudge](curatortvjudge.md).

## Tunable constants

Uncalibrated starting points, like the film engine's.

| Constant                                 | Value                    | What it gates                                                                     |
| ---------------------------------------- | ------------------------ | --------------------------------------------------------------------------------- |
| `MAX_DELETIONS_PER_RUN` (`planner.py`)   | 10                       | Rolling 7-day cap, largest first; `CLEANUP_MAX_DELETIONS` overrides it. Series are far larger than films. |
| `DEFAULT_JUDGE_LIMIT` (`planner.py`)     | 30                       | Candidates offered to the judge per run, largest first; `CLEANUP_JUDGE_LIMIT` overrides it. |
| `WINDOW_DAYS` (`evidence.py`)            | 180                      | Viewing window before a series can be a candidate.                                |
| `SHORT_WINDOW_DAYS` (`evidence.py`)      | 60                       | Window when the IMDb score is below 5.5.                                          |
| `KEEP_BAR_VOTES` / `KEEP_BAR_SCORE`      | 5000 votes / 6.5         | Rating bar for automatic protection and the `keep` tag, as for films.             |
| `COMPLETION_PERCENT` (`evidence.py`)     | 85%                      | An episode watched this far counts as finished.                                   |
| `DEFAULT_RECONSIDER_DAYS` (`planner.py`) | 180                      | How long a review decision stands before re-judging.                              |
| `ANOMALY_LIMITS` (`planner.py`)          | 10% / 2% / 5% / 20% / 1% | Library size / keep tags / exclusions / history rows / invalid added dates.       |

## CronJob timing

`curatortv` runs Saturdays at 09:00 and `curatortvjudge` at 09:30, an hour after the film pair
(08:00 / 08:30), so the two never walk Tautulli's history at the same time.

## Alerting

`CuratortvPlanStale` and `CuratortvjudgeStale` (each app's `prometheusrule.yaml`) fire when the
CronJob has not succeeded in 8 days, the GickupBackupStale shape with a creation-time fallback. The
film pair has the same alerts, `CuratorPlanStale` and `CuratorjudgeStale`.
