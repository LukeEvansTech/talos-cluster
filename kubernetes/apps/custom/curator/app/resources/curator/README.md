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
the only command that can change anything. It deletes only when
`CLEANUP_MODE=act`; in `dry-run` it re-validates every safeguard, records its
intent, and stops short of the call. It adds the `keep` tag in `act` mode, and in
a dry run only when `TAG_KEEPS_IN_DRY_RUN=true`.

## What the engine decides

Every film in the library is assessed; there is no cut-off date unless
`CLEANUP_SCOPE_START` sets one. Each film gets one outcome:

| Outcome        | Meaning                                                                          |
| -------------- | -------------------------------------------------------------------------------- |
| `protected`    | a protection below applies; never offered to the judge                           |
| `out_of_scope` | not released, or added before a boundary that has been set                       |
| `missing_file` | nothing on disk to reclaim                                                       |
| `not_due`      | still inside its viewing window                                                  |
| `review`       | the engine could not assess it, or the judge asked for a person                  |
| `spared`       | a standing decision not to delete: an earlier judge keep, or `cleanup-dismissed` |
| `candidate`    | past its window, unprotected, with play history that can be trusted              |

**Protections, in this order.**

1. The allow list: the tags `keep`, `cleanup-keep` and the legacy `keep-review`.
   `keep` is applied by the rating bar and by every keep the judge hands down;
   `cleanup-keep` only by a person.
2. The rating bar: IMDb 6.5 or higher on 5,000 or more votes. The
   `cleanup-eligible` tag switches the bar off for one film, and does nothing
   else.
3. A monitored collection.
4. Watched by the house: two or more people finished it. This rests on play
   history, so it is applied only once that history has been shown to be
   trustworthy, after the window; an untrustworthy count goes to a person.

**What is not a protection.** A request is not: a requested film waits out its
window like any other and then goes to the judge with `origin: requested` and
`requested_by` on its record. Nor is an unknown origin a reason to hold a film
back: a film with no import-list tag and no request is a candidate with
`origin: unknown`. Requiring an import-list tag once left all but two films of
the library undeletable and everything else in a queue nobody could clear. How a
film arrived is evidence for the judge, not a permission.

**The viewing window** is 90 days from the first import, 30 when the IMDb score
is under 5.5.

**What reaches a person** is only what the engine cannot assess (no usable added
date, availability unknown, play history unavailable, incomplete, unresolved or
ambiguous, collection data unavailable) and the judge's own `review` verdicts.
A film tagged `cleanup-dismissed` is `spared` until a fact about it changes, and
is not listed as waiting.

**Judging is budgeted.** `plan` hands the judge the largest candidates first, up
to `CLEANUP_JUDGE_LIMIT`, as `candidates`; the rest are written to
`judge_deferred`. A deferred candidate was never offered, so no verdict is owed
for it and it comes back next run. Only an _offered_ candidate with no usable
verdict counts as unjudged.

**What a verdict does.** `delete` is executed, largest first, up to
`CLEANUP_MAX_DELETIONS` in any rolling seven days. `keep` adds the `keep` tag,
so the film joins the allow list for good; the tag is confirmed by re-reading,
and a keep whose tag did not land is not recorded as settled, so it is judged
and tagged again next run and the run exits non-zero. `review` goes to the
digest and stays there until a person acts or the decision lapses.

## Exit codes

`execute` exits non-zero for anything that should not read as a successful week,
which is what the staleness alert keys on. The CronJob's shell keeps the first
non-zero code and still sends the digest.

| Code | Meaning                                                            |
| ---- | ------------------------------------------------------------------ |
| 2    | the plan recorded blocking issues                                  |
| 3    | another run holds the execute lock                                 |
| 4    | a deletion failed or could not be confirmed                        |
| 5    | refused: `--read-only` in act mode, or a stale or wrong-mode plan  |
| 7    | the recommendations were judged against a different plan           |
| 8    | a `keep` tag could not be confirmed on re-read                     |
| 9    | an offered candidate came back unjudged, or a verdict was rejected |
| 10   | a deletion was skipped because a safety check could not be read    |

`report` exits 6 when the digest could not be delivered. A setting that cannot
be read (`CLEANUP_MAX_DELETIONS`, `CLEANUP_JUDGE_LIMIT`, `CLEANUP_SCOPE_START`)
stops `plan` or `execute` with exit 1 before anything is fetched or written.

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
import exclusion is worth more than an unattended batch of deletions a week.

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
| `CLEANUP_SCOPE_START`                                                                  | optional ISO date; films added before it are out of scope. Unset: no limit  |
| `CLEANUP_JUDGE_LIMIT`                                                                  | candidates `plan` offers the judge per run, largest first; default 150      |
| `CLEANUP_MAX_DELETIONS`                                                                | deletions `execute` allows in a rolling seven days; default 30              |
| `TAG_KEEPS_IN_DRY_RUN`                                                                 | `true` lets a dry run add the `keep` tag; deletion unaffected               |
| `CLEANUP_RUN_ID`                                                                       | optional; generated if absent                                               |

## What each module is responsible for

| Module         | Responsibility                                                                          |
| -------------- | --------------------------------------------------------------------------------------- |
| `model.py`     | value types, the tag vocabulary, timestamp parsing                                      |
| `clients.py`   | Radarr / Tautulli / request-system HTTP, with result-level validation and pagination    |
| `evidence.py`  | provenance, first-playable availability, viewing evidence                               |
| `planner.py`   | scope, protections, the judge limit and deletion cap, anomaly detection, the allow list |
| `ledger.py`    | durable state in object storage                                                         |
| `reporting.py` | the weekly digest, and its delivery                                                     |
| `executor.py`  | recommendation validation, live re-checks, deletion, reconciliation                     |
| `provision.py` | one-off: per-list provenance tags and the decision tags                                 |

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
exactly like "nobody asked for any of this", so the judge would be told the
wrong origin for every requested film, and a keep it hands down on that is a
permanent tag. Execution cannot make up for it either: it only re-reads requests
made _since_ the plan. The same rule is why a `get_metadata`
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
finished since the plan — were read once, before the loop. A batch of deletions
takes minutes; starting a film takes seconds. So both are re-read immediately
before each deletion, and an answer that cannot be got is a refusal rather than
a pass. A request made _before_ the plan is not a refusal: it is on the
candidate's record. A film can be requested more than once, so it is the newest
request that is compared with the plan, not the one that brought the film in.

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
