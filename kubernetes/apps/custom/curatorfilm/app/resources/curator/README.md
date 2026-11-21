# curator

The deterministic half of the weekly Radarr library cleanup. Judgement — is this
film worth keeping? — belongs to the `curatorfilmjudge` app next door. Everything
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

`plan` changes nothing in Radarr, and `report` changes nothing at all; it
publishes a review document only when a receiver is configured. `execute` is the only command that can change the library. It
deletes only when `CLEANUP_MODE=act`; in `dry-run` it re-validates every
safeguard, records its intent, and stops short of the call. It adds the `keep`
tag in `act` mode, and in a dry run only when `TAG_KEEPS_IN_DRY_RUN=true`.

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
| `proposed`     | a verdict is on record and waiting out its review window; not judged again       |
| `candidate`    | past its window, unprotected, with play history that can be trusted              |

**Protections, in this order.**

1. The allow list: the tags `keep`, `cleanup-keep` and the legacy `keep-review`.
   `keep` is applied by the rating bar and by every keep the judge hands down;
   `cleanup-keep` only by a person.
2. The rating bar: IMDb 6.5 or higher on 5,000 or more votes. The
   `cleanup-eligible` tag switches the bar off for one film: it is a person's
   delete (below), and it also keeps the `keep` tag off the film.
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
A film tagged `cleanup-dismissed` is `spared` and is not listed as waiting. A
changed fact reopens it only where the judge had already recorded a decision
for the film, because that record is where the facts are kept; a dismissal with
nothing on record stands until the tag is removed.

**Judging is budgeted.** `plan` hands the judge the largest candidates first, up
to `CLEANUP_JUDGE_LIMIT`, as `candidates`; the rest are written to
`judge_deferred`. A deferred candidate was never offered, so no verdict is owed
for it and it comes back next run. Only an _offered_ candidate with no usable
verdict counts as unjudged.

**What a verdict does.** Nothing, in the run that gives it. A `delete` or a
`keep` is written to the ledger as a proposal, with the facts it rested on and a
due date `CLEANUP_GRACE_DAYS` later (6, so the next weekly run), and is listed
in the digest's review window and the review document for the days in between.
`review` is recorded as a decision at once, goes to the digest, and stays there
until a person acts or the decision lapses.

**What a due proposal does.** A film with a proposal on record has the outcome
`proposed`: it is listed in the plan's `proposed`, not sent to the judge again,
and `execute` acts on it once it is due, provided the ledger holds the same
verdict the plan lists. A due `delete` goes through every live re-check and is
deleted, a person's deletes first and then largest first, up to
`CLEANUP_MAX_DELETIONS` a run. A due `keep` gets the `keep` tag, so the film
joins the allow list for good; the tag is confirmed by re-reading, and a keep
whose tag did not land stays a proposal, so it is tried again next run and the
run exits non-zero. A dry run deletes nothing, so its proposals stay on record
under the `dry-run/` prefix and are reported again each week.

**When a proposal lapses.** It stands only while the film would otherwise still
be a candidate and its fact fingerprint and play count are unchanged. A
protection that now applies, play history that can no longer be trusted,
somebody playing the film or its collection changing all end it. The plan lists
these as `lapsed_proposals`, `execute` removes them, and a film that is a
candidate again is judged afresh and waits again.

**A person outranks the judge, through two tags.** `cleanup-keep` protects a
film for good, which ends any proposal for it. `cleanup-eligible` is a person's
delete: the film is never sent to the judge, becomes a delete proposal due at
the next run, and goes ahead of the judge's own under the cap. It overrides the
rating bar, a keep the judge proposed, and a spare or a review the judge
recorded. It does not override `keep`, `cleanup-keep`, `keep-review`, a
monitored collection, the viewing window, two completers, play history that
cannot be trusted, or any live re-check; beside `cleanup-dismissed`, the
dismissal wins. Taking the tag off withdraws it, up to the moment of deletion:
`execute` re-reads the tag in its last live check of each title a person marked.

No `keep` tag, from the rating bar or from the judge, is ever put on a film
carrying `cleanup-eligible`, `cleanup-dismissed` or `keep-review`: those are a
person's decision, and `keep` would overrule it. A judge keep on such a film is
recorded as a decision that lapses after 180 days or when a fact changes, and
nothing more.

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

`report` exits 6 when the digest could not be delivered, and 11 when a review
page is configured and did not take the document (or the proposals for it
could not be read). With no review page configured it exits 12 instead when the
proposals could not be read, since the digest then cannot say what falls due.
A setting that cannot be read
(`CLEANUP_MAX_DELETIONS`, `CLEANUP_GRACE_DAYS`, `CLEANUP_JUDGE_LIMIT`,
`CLEANUP_SCOPE_START`) stops `plan` or `execute` with exit 1 before anything is
fetched or written.

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

The digest leads with the number a person can act on: its title says how many
titles go at the next run and on what date (`would go` in a dry run), and its
text lists every proposal on record (going, waiting and being kept) with its
size, due date and reason, uncut, since without a review page it is the only
full list. That count comes from the review
document below, which `report` builds on every run.

## The review document

`report` builds the review document from the proposals in the ledger, not from
this run's result, so a run that was blocked or whose `execute` never finished
still lists every verdict that is waiting. Publishing it is optional:

- With `CLEANUP_REVIEW_URL` unset, nothing is sent, the digest mentions no page,
  and the push carries no link.
- With it set, `report` renders the digest, puts it in the document, and posts
  the document there before it delivers the digest. The push links to
  `CLEANUP_REVIEW_LINK` when that is set as well. A receiver that refuses the
  document or cannot be reached is named in the digest, and `report` exits 11.
  Both judges' manifests set it to the `curator` app in the same namespace.

Both engines build the document with the same `review.py`, so this contract
covers the `tv` library too. Everything below is what a receiver can rely on;
`tests/review_example.json` is the example, and a test asserts the builder
produces it.

**Request.** `POST <CLEANUP_REVIEW_URL>/publish/<library>`, where `library` is
`film` from this engine and `tv` from `curatortv`, with
`Content-Type: application/json`. Any 2xx answer is success. Anything else, or
no answer within 30 seconds, fails the publish; it is not retried, and the next
week's document replaces it.

**Body.** One JSON object, serialised compactly (no whitespace), with its keys
sorted and every non-ASCII character escaped as `\uXXXX`, so the body is ASCII.

**Signature.** Header `X-Curator-Signature: sha256=<hex>`, where `<hex>` is the
lowercase hex HMAC-SHA256 of these bytes: the library name in UTF-8, one newline
(`0x0A`), then the request body exactly as sent. The key is the library's arr
API key in UTF-8: Radarr's for `film`, Sonarr's for `tv`. Signing the library
name with the body means a document signed for one library does not verify as
the other's. A receiver computes the HMAC over the raw body before parsing it,
compares in constant time, and refuses an unknown library or `version`. With no
API key the engine signs nothing and the publish fails.

**Top-level fields.** Every field is always present.

| Field          | Type            | Meaning                                                                                  |
| -------------- | --------------- | ---------------------------------------------------------------------------------------- |
| `version`      | integer         | `2`. Version 1 had every field here except `summary`                                     |
| `library`      | string          | `film` or `tv`, the same as the path                                                     |
| `generated_at` | string          | ISO 8601 UTC time `report` built the document                                            |
| `run_id`       | string          | the plan's run ID; empty when this run has no current plan                               |
| `mode`         | string          | `act` or `dry-run`                                                                       |
| `blocks`       | array of string | why this run acted on nothing (a blocked, missing or stale plan); empty in a normal run  |
| `cap`          | integer         | deletions allowed per run                                                                |
| `next_run_at`  | string          | ISO 8601 UTC time of the next weekly run: seven days after this run's plan, or after now |
| `going`        | array of title  | due deletes the next run takes, in its order (a person's first, then largest), to `cap`  |
| `later`        | array of title  | deletes past the cap, then deletes not due by the next run, largest first                |
| `kept`         | array of title  | keep proposals, largest first; each is tagged `keep` once due                            |
| `needs_you`    | array of title  | what the engine could not assess and the judge's `review` verdicts, largest first        |
| `deleted`      | array of title  | this run's deletions, or would-be deletions in a dry run                                 |
| `summary`      | object          | how the run went (version 2; below)                                                      |

A title appears in at most one of `going`, `later`, `kept` and `needs_you`.

**A title.** Every field is always present. Text fields come from metadata
providers and from a model: treat them as untrusted and escape them on display.

| Field          | Type            | Meaning                                                                                 |
| -------------- | --------------- | --------------------------------------------------------------------------------------- |
| `id`           | integer         | Radarr movie ID or Sonarr series ID                                                     |
| `title`        | string          | up to 300 characters; `#<id>` when the record has none                                  |
| `year`         | number or null  | release year                                                                            |
| `size_gb`      | number or null  | size on disk when the verdict was given                                                 |
| `imdb_score`   | number or null  | IMDb rating                                                                             |
| `imdb_votes`   | number or null  | IMDb vote count                                                                         |
| `plays`        | number or null  | plays recorded when the verdict was given                                               |
| `finished_by`  | number or null  | distinct people who finished it (film) or finished an episode (TV)                      |
| `origin`       | string          | `feed`, `requested`, `unknown`, or empty                                                |
| `requested_by` | string          | who requested it, up to 80 characters; empty when nobody did or it is not known         |
| `overview`     | string          | synopsis, up to 400 characters                                                          |
| `verdict`      | string          | `delete`, `keep` or `review`                                                            |
| `by`           | string          | `judge`, `person` (a `cleanup-eligible` tag) or `engine`; empty on a `deleted` entry    |
| `reason`       | string          | the judge's reason, the person-tag note or the engine's blocker; up to 600 characters   |
| `proposed_at`  | string          | ISO 8601 time the proposal was made; empty outside `going`, `later` and `kept`          |
| `due_at`       | string          | ISO 8601 time it falls due; empty outside `going`, `later` and `kept`                   |
| `simulated`    | boolean         | `true` only on a `deleted` entry from a dry run                                         |
| `actions`      | array of string | what a receiver may offer: `keep` adds `cleanup-keep`, `delete` adds `cleanup-eligible` |

`actions` is `["keep"]` on a delete proposal, `["delete"]` on a keep proposal,
`["keep", "delete"]` on a judge's `review`, `["keep"]` on a title the engine
could not assess (`cleanup-eligible` does not override the check that stopped
it), and empty on a `deleted` entry.

**`summary`.** Every key is always present; a part is `null` where noted.

| Field                        | Type              | Meaning                                                                                  |
| ---------------------------- | ----------------- | ---------------------------------------------------------------------------------------- |
| `plan`                       | object or null    | the plan's funnel; `null` when this run has no current plan                              |
| `plan.counts`                | object of integer | titles per outcome, keyed by outcome name (see the table at the top); a zero is left out |
| `plan.titles`                | integer           | the sum of `counts`: the whole library                                                   |
| `plan.offered`               | integer           | candidates offered to the judge this run                                                 |
| `plan.deferred`              | integer           | candidates past the judge limit, left for a later run                                    |
| `plan.judge_limit`           | integer or null   | the judge limit in force                                                                 |
| `plan.proposed`              | integer           | titles with a standing proposal, not judged again                                        |
| `plan.lapsed`                | integer           | proposals the plan ended because a fact changed                                          |
| `plan.review`                | integer           | titles the plan put in front of a person                                                 |
| `plan.missing_file`          | integer           | titles with nothing on disk                                                              |
| `judge`                      | object            | the judge step                                                                           |
| `judge.status`               | string            | `judged`, `partial`, `failed`, `malformed`, `not asked`, or `missing` (no record)        |
| `judge.detail`               | string            | the judge step's own account, up to 600 characters                                       |
| `judge.batches`              | integer or null   | requests made to the gateway                                                             |
| `judge.failed_batches`       | integer or null   | requests that produced no usable verdicts                                                |
| `judge.input_tokens`         | integer or null   | input tokens across every batch that got a reply                                         |
| `judge.output_tokens`        | integer or null   | output tokens across every batch that got a reply                                        |
| `judge.model`                | string or null    | the gateway model group that answered                                                    |
| `executor`                   | object or null    | what `execute` did; `null` when it left no result for this plan                          |
| `executor.deleted`           | integer           | titles deleted                                                                           |
| `executor.would_delete`      | integer           | titles a dry run would have deleted                                                      |
| `executor.size_gb`           | number            | size of both together, to one decimal place                                              |
| `executor.skipped`           | integer           | due deletes set aside at the last check                                                  |
| `executor.failed`            | integer           | deletions the service refused                                                            |
| `executor.uncertain`         | integer           | deletions whose outcome could not be confirmed                                           |
| `executor.deferred`          | integer           | due deletes held back by the cap                                                         |
| `executor.keep_tags_applied` | integer           | `keep` tags confirmed this run, from the rating bar and from due judge keeps             |
| `digest`                     | object            | the digest exactly as delivered                                                          |
| `digest.title`               | string            | the push title                                                                           |
| `digest.message`             | string            | the push message, in Pushover's HTML subset                                              |
| `digest.text`                | string            | the full Markdown report `report` writes and logs                                        |

A judgement or a result left on the volume by an earlier run is not reported as
this run's: the judge part then reads `failed`, and `executor` is `null`.

**Example.** A dry run with one of each kind of title:

<!-- review-example -->

```json
{
  "version": 2,
  "library": "film",
  "generated_at": "2026-09-12T08:30:00+00:00",
  "run_id": "run-20260912T080000-a1b2c3",
  "mode": "dry-run",
  "blocks": [],
  "cap": 2,
  "next_run_at": "2026-09-19T08:00:00+00:00",
  "going": [
    {
      "id": 3,
      "title": "Marked By Hand",
      "year": 2014,
      "size_gb": 2.1,
      "imdb_score": 4.6,
      "imdb_votes": 2310,
      "plays": 0,
      "finished_by": 0,
      "origin": "feed",
      "requested_by": "",
      "overview": "A synopsis from the metadata provider.",
      "verdict": "delete",
      "by": "person",
      "reason": "Marked for deletion by a person (cleanup-eligible).",
      "proposed_at": "2026-09-05T08:30:00+00:00",
      "due_at": "2026-09-05T08:30:00+00:00",
      "simulated": false,
      "actions": ["keep"]
    },
    {
      "id": 1,
      "title": "Shark Lake 3",
      "year": 2014,
      "size_gb": 40.2,
      "imdb_score": 4.6,
      "imdb_votes": 2310,
      "plays": 0,
      "finished_by": 0,
      "origin": "feed",
      "requested_by": "",
      "overview": "A synopsis from the metadata provider.",
      "verdict": "delete",
      "by": "judge",
      "reason": "Straight-to-video sequel nobody has played.",
      "proposed_at": "2026-09-05T08:30:00+00:00",
      "due_at": "2026-09-11T08:30:00+00:00",
      "simulated": false,
      "actions": ["keep"]
    }
  ],
  "later": [
    {
      "id": 4,
      "title": "Late Arrival",
      "year": 2014,
      "size_gb": 8.5,
      "imdb_score": 4.6,
      "imdb_votes": 2310,
      "plays": 0,
      "finished_by": 0,
      "origin": "requested",
      "requested_by": "sam",
      "overview": "A synopsis from the metadata provider.",
      "verdict": "delete",
      "by": "judge",
      "reason": "Straight-to-video sequel nobody has played.",
      "proposed_at": "2026-09-12T08:10:00+00:00",
      "due_at": "2026-09-18T08:10:00+00:00",
      "simulated": false,
      "actions": ["keep"]
    }
  ],
  "kept": [
    {
      "id": 2,
      "title": "A Kept Classic",
      "year": 2014,
      "size_gb": 6.0,
      "imdb_score": 4.6,
      "imdb_votes": 2310,
      "plays": 0,
      "finished_by": 0,
      "origin": "feed",
      "requested_by": "",
      "overview": "A synopsis from the metadata provider.",
      "verdict": "keep",
      "by": "judge",
      "reason": "Cult standing; people would expect to find it.",
      "proposed_at": "2026-09-05T08:30:00+00:00",
      "due_at": "2026-09-11T08:30:00+00:00",
      "simulated": false,
      "actions": ["delete"]
    }
  ],
  "needs_you": [
    {
      "id": 8,
      "title": "Could Go Either Way",
      "year": null,
      "size_gb": 3.0,
      "imdb_score": null,
      "imdb_votes": null,
      "plays": null,
      "finished_by": null,
      "origin": "",
      "requested_by": "",
      "overview": "",
      "verdict": "review",
      "by": "judge",
      "reason": "Split audience.",
      "proposed_at": "",
      "due_at": "",
      "simulated": false,
      "actions": ["keep", "delete"]
    },
    {
      "id": 9,
      "title": "No Date",
      "year": null,
      "size_gb": 1.5,
      "imdb_score": null,
      "imdb_votes": null,
      "plays": null,
      "finished_by": null,
      "origin": "",
      "requested_by": "",
      "overview": "",
      "verdict": "review",
      "by": "engine",
      "reason": "record has no usable added date",
      "proposed_at": "",
      "due_at": "",
      "simulated": false,
      "actions": ["keep"]
    }
  ],
  "deleted": [
    {
      "id": 7,
      "title": "Gone Already",
      "year": 2011,
      "size_gb": 3.2,
      "imdb_score": null,
      "imdb_votes": null,
      "plays": null,
      "finished_by": null,
      "origin": "",
      "requested_by": "",
      "overview": "",
      "verdict": "delete",
      "by": "",
      "reason": "",
      "proposed_at": "",
      "due_at": "",
      "simulated": true,
      "actions": []
    }
  ],
  "summary": {
    "plan": {
      "counts": {
        "candidate": 2,
        "missing_file": 1,
        "not_due": 120,
        "proposed": 3,
        "protected": 5800,
        "review": 1,
        "spared": 40
      },
      "titles": 5967,
      "offered": 2,
      "deferred": 0,
      "judge_limit": 150,
      "proposed": 3,
      "lapsed": 1,
      "review": 1,
      "missing_file": 1
    },
    "judge": {
      "status": "judged",
      "detail": "2 recommendations in 41000ms",
      "batches": 1,
      "failed_batches": 0,
      "input_tokens": 48210,
      "output_tokens": 3120,
      "model": "claude-opus"
    },
    "executor": {
      "deleted": 0,
      "would_delete": 1,
      "size_gb": 3.2,
      "skipped": 1,
      "failed": 0,
      "uncertain": 0,
      "deferred": 0,
      "keep_tags_applied": 0
    },
    "digest": {
      "title": "Library cleanup: 2 would go on 19 Sep",
      "message": "<b>DRY-RUN</b> · 5,967 films · 2 candidates\n<b>2 would go on 19 Sep</b> unless you keep them",
      "text": "# Library cleanup\n\nRun run-20260912T080000-a1b2c3 · mode dry-run\n"
    }
  }
}
```

## Accepting a new baseline

A blocked plan keeps the old baseline, so a real, deliberate fall in the library
blocks every later run too. `CLEANUP_ACCEPT_BASELINE=<run ID>` names the blocked
run whose figures a person has read and accepts: the next `plan` takes that
run's stored snapshot as the baseline before it compares anything. A snapshot
that could not have been a baseline is refused, and one no newer than the
current baseline is ignored, so the setting is honoured once and is harmless to
leave in place.

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
| `CLEANUP_MAX_DELETIONS`                                                                | deletions `execute` allows per run, counted over six days; default 30       |
| `CLEANUP_GRACE_DAYS`                                                                   | days a verdict waits before `execute` acts on it; default 6                 |
| `CLEANUP_REVIEW_URL`                                                                   | where `report` publishes the review document; unset, nothing is published   |
| `CLEANUP_REVIEW_LINK`                                                                  | page the push opens; sent only when `CLEANUP_REVIEW_URL` is set too         |
| `CLEANUP_ACCEPT_BASELINE`                                                              | run ID of a blocked run whose snapshot becomes the baseline; honoured once  |
| `TAG_KEEPS_IN_DRY_RUN`                                                                 | `true` lets a dry run add the `keep` tag; deletion unaffected               |
| `CLEANUP_RUN_ID`                                                                       | optional; generated if absent                                               |

## What each module is responsible for

| Module         | Responsibility                                                                          |
| -------------- | --------------------------------------------------------------------------------------- |
| `model.py`     | value types, the tag vocabulary, timestamp parsing                                      |
| `clients.py`   | Radarr / Tautulli / request-system HTTP, with result-level validation and pagination    |
| `evidence.py`  | provenance, first-playable availability, viewing evidence                               |
| `planner.py`   | scope, protections, the judge limit and deletion cap, anomaly detection, the allow list |
| `proposals.py` | the review window: when a verdict stands, when it is due, what goes on record           |
| `acting.py`    | what `execute` does around a deletion: tags, proposals, decisions, an accepted baseline |
| `review.py`    | the review document and its summary, and publishing it signed                           |
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
