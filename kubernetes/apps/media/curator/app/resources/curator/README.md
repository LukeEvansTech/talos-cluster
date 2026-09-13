# curator

The deterministic half of the weekly Radarr library cleanup. Judgement — is this
film worth keeping? — belongs to the `curatorjudge` app next door. Everything
that has to be _reliable_ rather than tasteful lives here.

The split exists because the two halves fail differently. A wrong judgement costs
one film. A wrong protection check costs a film somebody explicitly protected, it
happens silently, and the run reports success either way.

## Commands

Run from the directory above this one, which is the package root.

```bash
python3 -m curator plan --out ./cleanup-plan      # read-only, always safe
python3 -m curator execute --plan ./cleanup-plan/plan.json \
    --recommendations ./recommendations.json --out ./result.json --read-only
python3 -m curator report --plan ./cleanup-plan/plan.json \
    --result ./result.json --out ./report.md      # read-only, always safe
python3 -m curator selftest                       # fixture tests, no network
python3 -m curator.provision                      # show provenance-tag changes
```

`plan` and `report` write nothing outside their own output files. `execute` is
the only command that can change anything, and only when `CLEANUP_MODE=act`; in
`dry-run` it re-validates every safeguard, records its intent, and stops short of
the call.

## Scheduled runs are read-only

The CronJobs pass `--read-only`, which refuses to act whatever `CLEANUP_MODE`
says. Deleting on a schedule therefore takes two deliberate edits in different
places, and a half-made change refuses loudly instead of deleting quietly.

This is not a security boundary — anyone who can change one file can change both.
It is a guard against the change nobody meant to make, and it reflects where the
defects in this system have actually been: not in the judgement, but in the joins
between four services that disagree about what identifies a film. Every one of
those was a protection that read correctly, passed its tests, and could not see
part of the population it guarded. A person between that and an irreversible
import exclusion is worth more than an unattended thirty films a week.

## The digest is the output

`report` is what a read-only run is _for_. It renders the plan and the result
into a Markdown report and pushes a summary to a webhook, on every run — including
the runs where nothing happened, so that silence means the job did not run rather
than that it had nothing to say. Four things it refuses to paper over: an absent
plan, a plan left over from an earlier week, a result belonging to a different
run ID, and a judgement that was never obtained.

## Configuration

All of it from the environment, because this repository is public and a hostname
committed here maps the private network.

| Variable                                                                               | Meaning                                                                     |
| -------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `RADARR_URL`, `RADARR_API_KEY`                                                         | Radarr instance                                                             |
| `TAUTULLI_URL`, `TAUTULLI_API_KEY`                                                     | play history; a hard dependency                                             |
| `SEERR_URL`, `SEERR_API_KEY`                                                           | request system; optional to configure, but blocking once it is              |
| `LEDGER_DIR`                                                                           | durable state on a mounted volume; preferred over the S3 variables below    |
| `LEDGER_ENDPOINT`, `LEDGER_BUCKET`, `LEDGER_ACCESS_KEY_ID`, `LEDGER_SECRET_ACCESS_KEY` | S3-compatible store for durable state                                       |
| `CLEANUP_WEBHOOK_URL`, `CLEANUP_WEBHOOK_TOKEN`                                         | where `report` delivers the digest; without the URL it only writes the file |
| `CLEANUP_MODE`                                                                         | `dry-run` (default) or `act`                                                |
| `CLEANUP_SCOPE_START`                                                                  | ISO date; defaults to `2026-03-01`                                          |
| `CLEANUP_RUN_ID`                                                                       | optional; generated if absent                                               |

## What each module is responsible for

| Module         | Responsibility                                                                         |
| -------------- | -------------------------------------------------------------------------------------- |
| `model.py`     | value types, the tag vocabulary, timestamp parsing                                     |
| `clients.py`   | Radarr / Tautulli / request-system HTTP, with result-level validation and pagination   |
| `evidence.py`  | provenance, first-playable availability, viewing evidence                              |
| `planner.py`   | scope, protections, authorisation, batching, anomaly detection, allow-list maintenance |
| `ledger.py`    | durable state in object storage                                                        |
| `reporting.py` | the weekly digest, and its delivery                                                    |
| `executor.py`  | recommendation validation, live re-checks, deletion, reconciliation                    |
| `provision.py` | one-off: per-list provenance tags and the decision tags                                |

## Eight things that are easy to get wrong

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

**An outage is not an answer.** A request system that is configured and does not
respond blocks the run rather than degrading it. An empty request set reads
exactly like "nobody asked for any of this", and for a film carrying an
import-list provenance tag that difference is the entire authorisation to delete
it — execution only re-reads requests made _since_ the plan, so a request made
before an outage would never be seen again. The same rule is why a `get_metadata`
timeout is now raised rather than returned as "no such item": swallowed, it let
the active-session check quietly drop the film somebody was watching. The
sharpest version is an outage written into a _durable_ cache: the rating-key
crosswalk is only consulted for keys it does not already hold, so a timeout
recorded there is never asked about again. Those plays can then only be matched
back by title, which finds nothing when Plex and Radarr disagree about one — and
the film reads as never watched, permanently, on the strength of one bad minute.
An authoritative "no such item" is cached; a failure to ask is not.

**A history row that is still playing carries no row ID.** Tautulli lists the
in-progress session in `get_history` and counts it in `recordsFiltered`, but the
row has `row_id: null` until playback stops. De-duplicating it away left
`retrieved` one short of the declared count, which is the engine's definition of
unusable history — so a run that happened while somebody was watching a film
blocked completely and did nothing. Found live on 2026-09-12 rather than by any
test, because it only appears while a session is open.

**Every file on the state volume outlives the run that wrote it.** The plan, the
verdicts, the result and the judgement all sit on one PersistentVolumeClaim from
one Saturday to the next, so a step that dies before rewriting its output leaves
last week's copy exactly where this week's would be. A judging step that crashed
on a malformed answer used to leave last week's `delete` verdicts in place, and
the shell deliberately continues to `execute` — which, in act mode, would delete
any of those films still on this week's candidate list. Two habits follow: the
refusal is written _first_, before anything that can raise, and every output
names the `run_id` of the plan it belongs to. A file that names another plan is
refused rather than read.

**A snapshot taken before a batch is not evidence about the batch's last film.**
Live re-validation covers everything Radarr holds, per film. The two checks that
live outside Radarr — who is watching right now, and what has been requested or
finished since the plan — were read once, before the loop. A batch of up to
thirty deletions takes minutes; starting a film takes seconds. So both are
re-read immediately before each deletion, and an answer that cannot be got is a
refusal rather than a pass.

## The first in-cluster run

`LEDGER_DIR` points at a fresh PersistentVolumeClaim, so the ledger does not carry
over from the S3 store the cloud routine used. That is deliberate but not free:
the first run finds no validated baseline, blocks on that, records one, and
deletes nothing. It is the designed behaviour and it self-corrects on the second
run.

What is lost is the rating-key crosswalk, which rebuilds itself in seconds, and
the spare decisions from the dry-run era, which get re-judged. Nothing dangerous
is lost: outstanding deletion intents cannot exist, because nothing has ever run
in `act` mode and dry-run state is namespaced under its own prefix.

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
