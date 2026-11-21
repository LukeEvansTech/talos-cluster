# Curatortvjudge

The taste half of the weekly Sonarr library cleanup, in the `custom` namespace. It is
[Curatorfilmjudge](curatorfilmjudge.md) pointed at the TV engine: the same three steps (`prompt`, `judge`,
`app`), the same `judge.py` and `read_verdicts.py`, the same hardening, `backoffLimit: 0`, a judge
that never mounts the state volume, and an executor run with `--read-only` and
`CLEANUP_MODE: dry-run`. Read that page for the request to the gateway, the files between the
steps, the trust boundary and the failure semantics; this page covers what differs.

## What differs from Curatorfilmjudge

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
  `CLEANUP_MAX_DELETIONS` is 10, the engine's own default. `CLEANUP_GRACE_DAYS` is 6 and
  `CLEANUP_KEEP_DAYS` is 365, as for films.
- **Review document:** built from the series proposals as the `tv` library. Like the film
  judge's, it is published only when `CLEANUP_REVIEW_URL` is set, to `/publish/tv` signed with the
  Sonarr API key. This manifest sets it to the [Curator](curator.md) Service, and the push opens
  `https://curator.${SECRET_DOMAIN}/`.
- **Digest:** same webhook as Curatorfilmjudge; every title starts `TV cleanup` so the two are
  distinguishable on the phone.

## Three tiers of keeping

As for films ([Curatorfilm](curatorfilm.md#three-tiers-of-keeping)):

- **Keep for good:** a person (`cleanup-keep`), the rating bar, or an existing `keep` tag. Never
  judged again.
- **Keep for a year:** the judge keeps the series. Once its review week is over it becomes a
  stored decision, kept until a date `CLEANUP_KEEP_DAYS` (365) ahead, and the plan reports it as
  `spared`. It is judged again after that date, or sooner when a fact it rested on changes
  (somebody watched an episode). No tag is written.
- **Normal:** everything else, through the weekly cycle.

During the review week the review page offers a kept series both Keep forever (`cleanup-keep`) and
Delete instead (`cleanup-eligible`). The result file reports the judge's due keeps as
`judge_keeps`, and the digest says each is kept for a year.

## Rating-bar keep tags are written in a dry run

`TAG_KEEPS_IN_DRY_RUN: "true"` lets the `app` step add the `keep` tag to rating-bar series even
though the run is a dry run with `--read-only`. A judge's keep is never a tag, so the flag does
not touch it. The film engine supports the same flag and its manifest leaves it off, so this is
the one place the TV pipeline writes where the film pipeline does not. It exists for Reclaimerr,
which spares anything tagged `keep`: without it, a dry run leaves Reclaimerr blind to the rating
bar's decisions.

- It is add-only, so it is undone by removing the tag.
- Each series' live tags are re-read first; a series carrying `cleanup-eligible`,
  `cleanup-dismissed` or `keep-review` is never tagged.
- Deletion is unaffected: still impossible in dry-run mode or under `--read-only`.

The result file reports these writes as `keep_tags_added`, and the digest lists them. Each tag is
confirmed by re-reading the series: one that did not land is tried again next week, and the run
exits non-zero.

## Shared credentials

The gateway key is the same LiteLLM virtual key the film judge uses, read from the `litellm`
1Password item as `LITELLM_CURATOR_API_KEY` into this app's own `curatortvjudge-gateway` Secret, so
both judges draw on one key and one budget. The digest token is `chaski`'s `TRIAGE_WEBHOOK_TOKEN`,
as for films.
