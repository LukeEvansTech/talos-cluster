# Curatortvjudge

The taste half of the weekly Sonarr library cleanup, in the `custom` namespace. It is
[Curatorjudge](curatorjudge.md) pointed at the TV engine: the same three steps (`prompt`, `judge`,
`app`), the same `judge.py` and `read_verdicts.py`, the same hardening, `backoffLimit: 0`, a judge
that never mounts the state volume, and an executor run with `--read-only` and
`CLEANUP_MODE: dry-run`. Read that page for the request to the gateway, the files between the
steps, the trust boundary and the failure semantics; this page covers what differs.

## What differs from Curatorjudge

- **Inputs:** reads the plan from the `curatortv` volume and runs `python3 -m curatortv` from the
  `curatortv-code` ConfigMap.
- **Rubric:** `taste.md` has two defaults. Reality, dating, competition and lifestyle series
  default to **delete** when nobody is watching them, whatever their score. Everything else
  defaults to **keep**, where the film rubric defaults to delete: a delete needs a nameable,
  series-specific reason (poorly received and unwatched, a feed add that never caught on, a
  request nobody followed up). Indian-language series are always deleted. Size never decides. The
  engine protects every scripted series over the rating bar, so the judge sees series below it,
  series with no trusted rating, and reality series at any rating.
- **Verdicts:** the schema's id field is `series_id`. The batch list carries the field name, which
  is how the two apps share `judge.py` and `read_verdicts.py` unchanged.
- **Limits:** `CLEANUP_JUDGE_LIMIT` is 30 (set on `curatortv`), `CLEANUP_JUDGE_BATCH` is 15 and
  `CLEANUP_MAX_DELETIONS` is 10, the engine's own default. `CLEANUP_GRACE_DAYS` is 6, as for films.
- **Review document:** built from the series proposals as the `tv` library. Like the film
  judge's, it is published only when `CLEANUP_REVIEW_URL` is set, to `/publish/tv` signed with the
  Sonarr API key, and this manifest does not set it.
- **Digest:** same webhook as Curatorjudge; every title starts `TV cleanup` so the two are
  distinguishable on the phone.

## Keep tags are written in a dry run

`TAG_KEEPS_IN_DRY_RUN: "true"` lets the `app` step add the `keep` tag to rating-bar series, and to
every series whose `keep` from the judge has waited out its review window, even though the run is
a dry run with `--read-only`. The flag does not shorten that wait: in every mode a judge's keep is
a proposal first, so a person has the same week to refuse it as they have to refuse a delete. The
film engine supports the same flag and its manifest leaves it off, so this is the one place the TV
pipeline writes where the film pipeline does not. It exists for Reclaimerr, which spares anything
tagged `keep`: without it, a dry run leaves Reclaimerr blind to every decision made here.

- It is add-only, so it is undone by removing the tag.
- Each series' live tags are re-read first; a series carrying `cleanup-eligible`,
  `cleanup-dismissed` or `keep-review` is never tagged.
- Deletion is unaffected: still impossible in dry-run mode or under `--read-only`.
- A judge `keep` becomes permanent: `keep` is an absolute veto, so the series is not re-judged
  after the usual 180 days unless a person removes the tag.

The result file reports both writes as `keep_tags_added` and `judge_keeps_tagged`, and the digest
lists them. Each tag is confirmed by re-reading the series: a keep whose tag did not land is not
recorded as a decision and stays a proposal, so it is retried next week, and the run exits
non-zero.

## Shared credentials

The gateway key is the same LiteLLM virtual key the film judge uses, read from the `litellm`
1Password item as `LITELLM_CURATOR_API_KEY` into this app's own `curatortvjudge-gateway` Secret, so
both judges draw on one key and one budget. The digest token is `chaski`'s `TRIAGE_WEBHOOK_TOKEN`,
as for films.
