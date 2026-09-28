# Curator

The deterministic half of the weekly Radarr library cleanup, in the `media` namespace. Assesses
every film and writes `cleanup-plan/plan.json`; it never deletes or changes a tag. `curatorjudge`
reads that plan half an hour later and supplies the taste half of the decision.

## Purpose

The engine is a small Python app checked into `kubernetes/apps/media/curator/app/resources/curator/`,
shipped to both `curator` and `curatorjudge` as one ConfigMap. Its own
`app/resources/curator/README.md` is the primary design document: the eight things that are easy
to get wrong, the first in-cluster run, and how to undo a deletion. This page covers the operational
detail that sits above the readme: the constants, the CronJob schedule, and how an execution
failure is meant to be read.

## Tunable constants

None of these have a calibration history yet (the whole engine shipped in one PR, #5099); treat
them as starting points, not measured thresholds.

| Constant                                      | Value                    | What it gates                                                                                                                                                                    |
| --------------------------------------------- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MAX_DELETIONS_PER_RUN` (`planner.py`)        | 30                       | Rolling 7-day cap (`ledger.deletions_in_window` subtracts recent confirmed deletions before this applies), largest files first.                                                  |
| `DEFAULT_RECONSIDER_DAYS` (`planner.py`)      | 180                      | How long a spare or dismissal stands before re-judging.                                                                                                                          |
| `SUBJECT_EXACT_BELOW` (`planner.py`)          | 25                       | Genre/subject counts below this are fingerprinted exactly, above it bucketed, so a decision only reopens when the count materially changes.                                      |
| `ANOMALY_LIMITS` (`planner.py`)               | 10% / 2% / 5% / 20% / 1% | Drop in library size / keep-tagged films / import exclusions / play-history rows / invalid-added-date fraction that blocks a run as a probable break rather than ordinary drift. |
| `KEEP_BAR_VOTES` / `KEEP_BAR_SCORE`           | 5000 votes / 6.5         | IMDb rating bar for the permanent allow-list tag.                                                                                                                                |
| `COMPLETION_PERCENT` (`evidence.py`)          | 85%                      | Runtime watched to count as a completed play.                                                                                                                                    |
| `ABANDONED_PERCENT` (`evidence.py`)           | 20%                      | Below this is a bounce, not evidence of value.                                                                                                                                   |
| `PUSH_LIMIT` / `TITLE_LIMIT` (`reporting.py`) | 1000 / 240 characters    | Cut below Pushover's own 1024/250 limits, so the cut lands somewhere the report can mark it rather than where Pushover chooses.                                                  |

## CronJob timing

`curator` runs Saturdays at 08:00, `curatorjudge` at 08:30 (`app/helmrelease.yaml` in each app).
The 30-minute gap is a fixed schedule, not a wait on completion. `curator`'s
`activeDeadlineSeconds: 1800` is sized for a cold run, which resolves every new Plex rating key in
the identity crosswalk one at a time; a warm run (everything already cached) takes about fifteen
seconds.

## Execute failure semantics

`curatorjudge`'s CronJob (the one that runs `execute`) sets `backoffLimit: 0`. A Kubernetes Job
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
