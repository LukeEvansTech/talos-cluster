# Curatorfilm

The deterministic half of the weekly Radarr library cleanup, in the `custom` namespace. Assesses
every film and writes `cleanup-plan/plan.json`; it never deletes or changes a tag. `curatorfilmjudge`
reads that plan half an hour later and supplies the taste half of the decision, and nothing it
decides is acted on until it has waited a week in which a person can overrule it.

## Purpose

The engine is a small Python app checked into `kubernetes/apps/custom/curatorfilm/app/resources/curator/`,
shipped to both `curatorfilm` and `curatorfilmjudge` as one ConfigMap. Its own
`app/resources/curator/README.md` is the primary design document: the eight things that are easy
to get wrong, the first in-cluster run, and how to undo a deletion. This page covers the operational
detail that sits above the readme: the rules in brief, the constants, the CronJob schedule, and how
an execution failure is meant to be read.

## What it decides

The whole library is assessed; there is no cut-off date unless `CLEANUP_SCOPE_START` is set, and
the manifests do not set it. A film is protected, in this order, by:

1. the allow list: the tags `keep`, `cleanup-keep` or the legacy `keep-review`;
2. the rating bar: IMDb 6.5 or higher on 5,000 or more votes, unless it is tagged
   `cleanup-eligible`, which is a person's delete (see the review window below). Like
   `cleanup-dismissed` and `keep-review`, that tag keeps every automatic `keep` tag off the film,
   a judge keep's included;
3. a monitored collection;
4. the house having watched it: two or more people finished it.

Everything else that is released, on disk, and past its viewing window (90 days from first import,
30 when IMDb is under 5.5) is a candidate, provided its play history can be trusted. Neither a
request nor an unknown origin holds a film back any longer: `origin` (`feed`, `requested` or
`unknown`), `requested_by` and `original_language` go on the record the judge sees.

Only two things reach a person, as "Waiting on you" in the digest: what the engine could not
assess (no usable added date, availability unknown, play history unavailable, incomplete,
unresolved or ambiguous, collection data unavailable), and the judge's own `review` verdicts. A
film the judge kept earlier, or one tagged `cleanup-dismissed`, has the outcome `spared` and is not
listed. A dismissal is reopened by a changed fact only where the judge had already recorded a
decision for the film; one with nothing on record stands until the tag is removed.

The judge is offered the largest `CLEANUP_JUDGE_LIMIT` candidates each run (150 here, set on this
app's HelmRelease because the plan is where the split is recorded). The rest are written to the
plan as `judge_deferred`: they owe no verdict, do not fail the run, and are counted in the digest.

## The review window

A verdict is not acted on in the run that gives it. The judge's `delete` or `keep` is written to
the ledger as a proposal (`proposals/<movie_id>.json`) with the facts it rested on and a due date
`CLEANUP_GRACE_DAYS` later: 6, so the next weekly run. The digest lists what goes at the next
run, with the judge's reason, and doing nothing lets it proceed.

- The plan gives a film with a proposal on record the outcome `proposed` and lists it under
  `proposed`, not `candidates`. It is not judged again, so the judge limit is spent on films with
  no verdict yet.
- A proposal stands only while the film would otherwise still be a candidate, and its fact
  fingerprint and play count are what they were. Anything else ends it: a protection that now
  applies, play history that can no longer be trusted, somebody playing it, its collection
  changing. The plan lists these under `lapsed_proposals` and `execute` takes them off the record.
  A film that is a candidate again is judged afresh and waits again.
- `execute` acts only on a proposal that the plan upheld, that the ledger also holds with the same
  verdict and dates, and that is due. Due deletes go through every live re-check and the cap, a
  person's first and then largest first. Due keeps get the `keep` tag, and only a confirmed tag
  closes the proposal.
- A dry run records proposals under the `dry-run/` prefix and reports what it would have done to
  the due ones. Nothing is deleted, so they stay on record and are reported again the next week.

Two tags let a person overrule the judge, one in each direction. They are set in Radarr, and
anything built on the review document (below) should set exactly these and nothing else:

- `cleanup-keep` protects the film for good, which also ends any proposal for it.
- `cleanup-eligible` is a person's delete. The film is never sent to the judge: it becomes a
  delete proposal due at the next run, and goes ahead of the judge's own under the cap. It
  overrides the rating bar, a keep the judge proposed, and a spare or review the judge recorded.
  It does not override `keep`, `cleanup-keep`, `keep-review`, a monitored collection, the viewing
  window, two completers, play history that cannot be trusted, or any live re-check. Beside
  `cleanup-dismissed` the dismissal wins. Removing the tag withdraws it, up to the moment of
  deletion: the last live check before each person's delete re-reads the tag.

In `act` mode this means the first run records a baseline, the second makes proposals, and the
judge's first deletion happens a week after that, with every such title having been in a digest a
week earlier. A person's delete is the exception: `cleanup-eligible` is due at once, so a title
carrying it can go in the second run, and in any run after that, without having been in a digest
first. Take the tag off before the run to withdraw it.

## The review document

Each run's `report` step builds a review document from the proposals on record: what goes at the
next run, what waits behind the cap, what is being kept, what needs a person, what this run
deleted, and (version 2) a summary of the plan's funnel, the judge's batches and tokens, the
executor's totals and the digest as sent. The digest's going count comes from it.

Publishing it is optional. Set `CLEANUP_REVIEW_URL` on the judge's HelmRelease and `report` posts
it there, signed with the Radarr API key, and the push links to `CLEANUP_REVIEW_LINK` if that is
set too. The manifests set neither, so today nothing is published, the digest mentions no page, and
the push carries no link. The request, signature and every field are specified in the engine's
README, under "The review document", with a complete example the tests hold the builder to.

## Tunable constants

None of these have a calibration history yet (the whole engine shipped in one PR, #5099); treat
them as starting points, not measured thresholds.

| Constant                                      | Value                    | What it gates                                                                                                                                                                    |
| --------------------------------------------- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MAX_DELETIONS_PER_RUN` (`planner.py`)        | 30                       | Deletions per run, a person's deletes first and then largest files first; `ledger.deletions_in_window` subtracts confirmed deletions inside `CAP_WINDOW_DAYS`. `CLEANUP_MAX_DELETIONS` overrides it; `curatorfilmjudge` sets 100 while the first backlog drains. |
| `CAP_WINDOW_DAYS` (`planner.py`)              | 6                        | How far back the cap counts. A day short of the weekly schedule: last week's deletions were recorded minutes after the time this week's run counts from, so a 7-day window still held all of them and every second run deleted nothing. A second run in the same week still counts the first. |
| `DEFAULT_GRACE_DAYS` (`proposals.py`)         | 6                        | Days a verdict waits before `execute` acts on it. A day short of the schedule for the same reason; `CLEANUP_GRACE_DAYS` overrides it.                                            |
| `DEFAULT_JUDGE_LIMIT` (`planner.py`)          | 150                      | Candidates offered to the judge per run, largest first. `CLEANUP_JUDGE_LIMIT` overrides it.                                                                                      |
| `DEFAULT_RECONSIDER_DAYS` (`planner.py`)      | 180                      | How long a spare or dismissal stands before re-judging.                                                                                                                          |
| `SUBJECT_EXACT_BELOW` (`planner.py`)          | 25                       | Genre/subject counts below this are fingerprinted exactly, above it bucketed, so a decision only reopens when the count materially changes.                                      |
| `ANOMALY_LIMITS` (`planner.py`)               | 10% / 2% / 5% / 20% / 1% | Drop in library size / keep-tagged films / import exclusions / play-history rows / invalid-added-date fraction that blocks a run as a probable break rather than ordinary drift. |
| `KEEP_BAR_VOTES` / `KEEP_BAR_SCORE`           | 5000 votes / 6.5         | IMDb rating bar for the permanent allow-list tag.                                                                                                                                |
| `COMPLETION_PERCENT` (`evidence.py`)          | 85%                      | Runtime watched to count as a completed play.                                                                                                                                    |
| `ABANDONED_PERCENT` (`evidence.py`)           | 20%                      | Below this is a bounce, not evidence of value.                                                                                                                                   |
| `PUSH_LIMIT` / `TITLE_LIMIT` (`reporting.py`) | 1000 / 240 characters    | Cut below Pushover's own 1024/250 limits, so the cut lands somewhere the report can mark it rather than where Pushover chooses.                                                  |

## CronJob timing

`curatorfilm` runs Saturdays at 08:00, `curatorfilmjudge` at 08:30 (`app/helmrelease.yaml` in each app).
The 30-minute gap is a fixed schedule, not a wait on completion. `curatorfilm`'s
`activeDeadlineSeconds: 1800` is sized for a cold run, which resolves every new Plex rating key in
the identity crosswalk one at a time; a warm run (everything already cached) takes about fifteen
seconds.

## Alerting

`CuratorfilmPlanStale` and `CuratorfilmjudgeStale` (each app's `prometheusrule.yaml`) fire when the CronJob
has not succeeded in 8 days, the GickupBackupStale shape with a creation-time fallback. `execute`
exits 9 for a week in which an offered candidate went unjudged or a verdict was rejected, 8 when a
`keep` tag could not be confirmed, and 10 when a safety check could not be read; `report` exits 11
when a configured review page did not take the document, and 12 when no page is configured and
the proposals could not be read for the digest. None of them resets the alert; the README in
`resources/curator/` lists every exit code. Candidates deferred past the judge limit are not
unjudged and do not fail a run.

## Accepting a new baseline

A plan that finds the library has shrunk past an `ANOMALY_LIMITS` threshold blocks the run and
keeps the old baseline, so every later run is blocked too until somebody decides the fall was
real. When it was (a deliberate clear-out, say), set `CLEANUP_ACCEPT_BASELINE` on this app's
HelmRelease to the run id of the blocked run; the digest's Blocked section prints the line to use.
The next plan takes that run's stored snapshot as the baseline before it compares anything.

What is accepted is the set of figures a person has already read, not whatever the next run
finds. A snapshot that could not have been a baseline (unusable play history, say) is refused, and
one no newer than the current baseline is ignored, so the setting is honoured once and can be
removed whenever convenient.

## Execute failure semantics

`curatorfilmjudge`'s CronJob (the one that runs `execute`) sets `backoffLimit: 0`. A Kubernetes Job
with `backoffLimit: 0` does not retry, so a non-zero exit only marks the Job failed; it never
triggers Kubernetes itself to re-run the pipeline.

`cmd_execute` in `__main__.py` also exits non-zero for several routine refusals that happen before
any deletion is attempted: a blocked plan, a plan judged against a different plan's recommendations,
a stale or wrong-mode plan, or lock contention with another run. None of these are ambiguous or
destructive; they mean the run refused to start and nothing was touched. The distinction that
matters, covered below, is specific to what happens once deletion is under way: `cmd_execute`
distinguishes two different post-deletion outcomes, deliberately conflated into the same
Job-failure signal:

- **A failed deletion** (Radarr answered with a non-2xx status) is a safe no-op to retry: nothing
  was destroyed, so a human or a future run re-attempting it costs nothing.
- **An uncertain deletion** (a transport error after issuing the DELETE) is not safe to retry: the
  delete may have already landed, and `_delete_one` refuses to re-issue it. `_delete_one` still
  writes an "uncertain" outcome record immediately, so nothing automatically re-checks it later;
  it stays in the ledger at that status, the digest reports it, and the next `plan` run reads
  Radarr's actual live state fresh, independently of this record.

Surfacing both as a Job failure would have been unsafe if the CronJob could retry on its own, since
a retry would re-run judgement and could re-issue a DELETE that already landed. With
`backoffLimit: 0` that risk doesn't apply: an ambiguous destructive operation stays visible instead
of being silently retried.

`cmd_execute` also starts each run by calling `ledger.unreconciled_intents()`, which is a separate
mechanism from the uncertain-outcome case above: it finds intents from an _earlier_ run that never
got any outcome recorded at all, because the process died between writing the intent and
`_delete_one` finishing. `reconcile()` re-checks each of those against live Radarr state and
records a proper outcome for it. Because `_delete_one` always writes an outcome record for an
uncertain deletion, `unreconciled_intents()` does not pick those up; the two mechanisms cover
different gaps.
