# curator

The deterministic half of the weekly Radarr library cleanup routine. Judgement —
is this film worth keeping? — stays with Claude in the routine prompt. Everything
that has to be _reliable_ rather than tasteful lives here.

The split exists because the two halves fail differently. A wrong judgement costs
one film. A wrong protection check costs a film somebody explicitly protected, it
happens silently, and the run reports success either way.

## Commands

```bash
cd scripts
python3 -m curator plan --out ./cleanup-plan      # read-only, always safe
python3 -m curator execute --plan ./cleanup-plan/plan.json \
    --recommendations ./recommendations.json --out ./result.json
python3 -m curator selftest                       # fixture tests, no network
python3 -m curator.provision                      # show provenance-tag changes
```

`plan` writes nothing anywhere. `execute` is the only command that can change
anything, and only when `CLEANUP_MODE=act`; in `dry-run` it re-validates every
safeguard, records its intent, and stops short of the call.

## Configuration

All of it from the environment, because this repository is public and a hostname
committed here maps the private network.

| Variable                                                                               | Meaning                                                                              |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `RADARR_URL`, `RADARR_API_KEY`                                                         | Radarr instance                                                                      |
| `TAUTULLI_URL`, `TAUTULLI_API_KEY`                                                     | play history; a hard dependency                                                      |
| `SEERR_URL`, `SEERR_API_KEY`                                                           | request system; optional, but without it no film can be shown to have been requested |
| `LEDGER_ENDPOINT`, `LEDGER_BUCKET`, `LEDGER_ACCESS_KEY_ID`, `LEDGER_SECRET_ACCESS_KEY` | S3-compatible store for durable state                                                |
| `CLEANUP_MODE`                                                                         | `dry-run` (default) or `act`                                                         |
| `CLEANUP_SCOPE_START`                                                                  | ISO date; defaults to `2026-03-01`                                                   |
| `CLEANUP_RUN_ID`                                                                       | optional; generated if absent                                                        |

## What each module is responsible for

| Module         | Responsibility                                                                         |
| -------------- | -------------------------------------------------------------------------------------- |
| `model.py`     | value types, the tag vocabulary, timestamp parsing                                     |
| `clients.py`   | Radarr / Tautulli / request-system HTTP, with result-level validation and pagination   |
| `evidence.py`  | provenance, first-playable availability, viewing evidence                              |
| `planner.py`   | scope, protections, authorisation, batching, anomaly detection, allow-list maintenance |
| `ledger.py`    | durable state in object storage                                                        |
| `executor.py`  | recommendation validation, live re-checks, deletion, reconciliation                    |
| `provision.py` | one-off: per-list provenance tags and the decision tags                                |

## Four things that are easy to get wrong

**A zero play count has four meanings.** Only one of them is "nobody watched it".
The others are history that does not reach back far enough, an identity join that
failed, and a history service that did not answer. `HistoryStatus` keeps them
apart, and only `OK` permits a deletion.

**The identity join runs from history rows to films, not the other way round.**
A film nobody has played never appears in the history at all, so "not found in the
crosswalk" is the _expected_ state for exactly the films this routine exists to
remove. Treating it as a failed lookup puts every genuine candidate into review
forever. What does taint a zero is a play on a _retired_ Plex rating key, which
`get_metadata` 404s on: those rows are matched back by title and year, and any
film they might belong to goes to review.

**`added` is not availability.** Radarr records when it learned a film exists,
which can be years before a file arrives. The window runs from the first
`downloadFolderImported` event — the _first_, so a later quality upgrade cannot
restart a grace period that already ran.

**The exclusion, not the file, is the destructive half of a deletion.** The file
goes to the recycle bin and is recoverable while retention lasts. The exclusion
persists until someone removes it, and it blocks collection completion as well as
the import lists.

## Undoing a deletion

Recoverable while the recycle bin still holds the file. In order:

1. Re-add the movie in Radarr. The intent record written before the delete holds
   what you need to recreate it: `tmdbId`, `titleSlug`, `path`, `rootFolderPath`,
   `qualityProfileId`, `minimumAvailability`, `monitored` and the tag IDs.
2. Restore the file from the recycle bin to the film's folder and run a rescan so
   Radarr re-imports it.
3. Remove the import-list exclusion — `DELETE /api/v3/exclusions/<id>`, found by
   **tmdbId**; that endpoint has no `imdbId` field at all, so matching on IMDb ID
   silently returns nothing and reads exactly like "it was never excluded".

Retention is best-effort. A recycle bin can be shared with other tools and can be
emptied early, so the engine reads the configured path and retention every run
rather than assuming either.
