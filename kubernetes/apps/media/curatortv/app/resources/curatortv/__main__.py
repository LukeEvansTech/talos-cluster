"""Command-line entry point: ``plan``, ``execute``, ``report`` and ``selftest``.

``plan`` and ``report`` are read-only and always safe to run. ``execute`` is the
only command that can delete anything, and only when ``CLEANUP_MODE=act`` and it
was not given ``--read-only``. It can add the ``keep`` tag in a dry run when
``TAG_KEEPS_IN_DRY_RUN=true`` (see the README).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import reporting
from .clients import RequestSystem, Sonarr, SourceError, Tautulli
from .evidence import (
    build_crosswalk,
    has_external_id,
    resolve_availability,
    resolve_provenance,
    resolve_viewing,
)
from .executor import ExecutionResult, execute, reconcile, recycle_bin_ready, validate_recommendations
from .ledger import Ledger
from .model import (
    DEFAULT_FEED_TAGS,
    TAG_KEEP,
    Assessment,
    Availability,
    EpisodeProgress,
    HistoryStatus,
    Origin,
    Outcome,
    Provenance,
    Show,
    Viewing,
    utc,
)
from .planner import (
    MAX_DELETIONS_PER_RUN,
    PlanContext,
    assess,
    build_decision_record,
    check_anomalies,
    keep_tag_applies,
    keep_tag_targets,
    select_batch,
    snapshot_valid,
    summarise,
)
from .s3 import S3Client, S3Config
from .store import FileStore

DEFAULT_SCOPE_START = "2026-03-01"


def log(message: str) -> None:
    """Timestamped progress line on stdout."""
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}", flush=True)


def env(name: str, required: bool = True) -> str:
    """Read a configuration value from the environment.

    Hosts and keys are supplied by the caller, never baked in: this repository is
    public, and a hostname committed here maps the private network.
    """
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SystemExit(f"missing required environment variable {name}")
    return value


def feed_tags() -> frozenset[str]:
    """Tag labels that prove a feed added a series, from ``FEED_TAGS``."""
    raw = os.environ.get("FEED_TAGS", "")
    labels = frozenset(t.strip() for t in raw.split(",") if t.strip())
    return labels or DEFAULT_FEED_TAGS


def tag_keeps_in_dry_run() -> bool:
    """Whether a dry run may still add the ``keep`` tag."""
    return os.environ.get("TAG_KEEPS_IN_DRY_RUN", "").strip().lower() == "true"


def to_show(record: dict, tag_labels: dict[int, str]) -> Show:
    """Project a Sonarr series record onto the fields decisions depend on."""
    ratings = record.get("ratings") or {}
    stats = record.get("statistics") or {}
    votes = ratings.get("votes")
    value = ratings.get("value")
    files = int(stats.get("episodeFileCount") or 0)
    return Show(
        series_id=record["id"],
        tvdb_id=record.get("tvdbId") or None,
        tmdb_id=record.get("tmdbId") or None,
        imdb_id=record.get("imdbId") or None,
        title=record.get("title", ""),
        year=record.get("year") or None,
        status=record.get("status", ""),
        has_file=files > 0,
        size_bytes=int(stats.get("sizeOnDisk") or 0),
        added=utc(record.get("added")),
        tags=frozenset(tag_labels.get(t, str(t)) for t in record.get("tags", [])),
        imdb_score=float(value) if isinstance(value, (int, float)) and value else None,
        imdb_votes=int(votes) if isinstance(votes, int) and votes else None,
        genres=tuple(record.get("genres") or ()),
        network=record.get("network"),
        overview=record.get("overview") or "",
        episode_file_count=files,
        season_count=int(stats.get("seasonCount") or 0),
    )


@dataclass
class Sources:
    """Every client one run needs, plus its identity."""

    sonarr: Sonarr
    tautulli: Tautulli
    seerr: RequestSystem | None
    ledger: Ledger
    run_id: str


def build_store():
    """Pick the ledger backend from the environment.

    ``LEDGER_DIR`` is a mounted volume, which is how this runs in the cluster --
    the volume is snapshotted, so the ledger is backed up with everything else.
    Falling back to S3 keeps the engine runnable somewhere without one.
    """
    directory = env("LEDGER_DIR", required=False)
    if directory:
        return FileStore(directory)
    return S3Client(
        S3Config(
            endpoint=env("LEDGER_ENDPOINT"),
            access_key=env("LEDGER_ACCESS_KEY_ID"),
            secret_key=env("LEDGER_SECRET_ACCESS_KEY"),
            bucket=env("LEDGER_BUCKET"),
        )
    )


def build_sources() -> Sources:
    """Construct every client from the environment."""
    run_id = os.environ.get("CLEANUP_RUN_ID") or (
        f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    )
    seerr_url = env("SEERR_URL", required=False)
    return Sources(
        sonarr=Sonarr(env("SONARR_URL"), env("SONARR_API_KEY")),
        tautulli=Tautulli(env("TAUTULLI_URL"), env("TAUTULLI_API_KEY")),
        seerr=(RequestSystem(seerr_url, env("SEERR_API_KEY", required=False)) if seerr_url else None),
        ledger=Ledger(
            build_store(),
            run_id=run_id,
            dry_run=os.environ.get("CLEANUP_MODE", "dry-run").lower() != "act",
        ),
        run_id=run_id,
    )


def fetch_history(tautulli: Tautulli) -> tuple[list[dict], dict, bool, str]:
    """Play history, with an honest account of whether it can be trusted."""
    try:
        tautulli.preflight()
        rows, meta = tautulli.episode_history()
    except SourceError as exc:
        log(f"play history UNAVAILABLE: {exc}")
        return (
            [],
            {"declared": None, "retrieved": 0, "complete": False},
            False,
            str(exc),
        )

    log(f"play history: {meta['retrieved']} episode rows (declared {meta['declared']})")
    if not meta["complete"]:
        return rows, meta, False, "retrieved fewer rows than the service declared"
    if not rows:
        # A wiped or fresh Tautulli answers cleanly with zero rows, reading as complete;
        # nobody having watched anything isn't a state this library is ever in.
        return rows, meta, False, "history is healthy but empty, which is not a believable state"
    return rows, meta, True, ""


def group_by_show(rows: list[dict]) -> tuple[dict[int, list[dict]], list[dict]]:
    """Episode rows keyed by their show's rating key, plus the rows with none.

    A row that names no show is still a real play; it is returned separately so
    it can taint any series sharing its title rather than vanishing.
    """
    by_key: dict[int, list[dict]] = {}
    unkeyed: list[dict] = []
    for row in rows:
        key = row.get("grandparent_rating_key")
        if key in (None, "") or not str(key).isdigit():
            unkeyed.append(row)
            continue
        by_key.setdefault(int(str(key)), []).append(row)
    return by_key, unkeyed


def resolve_identities(
    tautulli: Tautulli, ledger: Ledger, rows_by_key: dict[int, list[dict]]
) -> tuple[dict[int, dict], list[dict]]:
    """Map Plex show rating keys to external ids, remembering the ones that cannot be.

    Plex reissues a rating key when an item is replaced and ``get_metadata`` 404s
    on the old one, but the plays behind it were still real plays. Those are kept
    and matched back by title, so they taint a zero rather than vanishing from it.
    """
    crosswalk = ledger.read_crosswalk()
    # A cached id-less entry that is not a retired key predates id-less entries being kept out
    # of the cache; ask again, since Plex may have matched the item since.
    missing = [
        k
        for k in rows_by_key
        if k not in crosswalk or not (has_external_id(crosswalk[k]) or crosswalk[k].get("unresolved"))
    ]
    log(f"resolving {len(missing)} new Plex show rating keys ({len(crosswalk)} cached)")

    fresh: dict[int, dict] = {}
    retired: dict[int, dict] = {}
    unreachable: dict[int, dict] = {}
    for key in missing:
        answered = True
        try:
            meta = tautulli.metadata(key)
        except SourceError as exc:
            # An infrastructure failure isn't evidence the key retired, but isn't reason
            # to abort either; marking it unresolved taints the zero and sends the show to review.
            log(f"  rating key {key} could not be resolved ({exc}); treating it as unresolved")
            meta, answered = None, False
        if meta:
            fresh[key] = meta
            continue
        sample = rows_by_key[key][0]
        entry = {
            "tvdb": None,
            "tmdb": None,
            "imdb": None,
            "title": sample.get("grandparent_title"),
            # An episode row's own year is the episode's, not the show's.
            "year": sample.get("grandparent_year"),
            "unresolved": True,
        }
        (retired if answered else unreachable)[key] = entry
    built = build_crosswalk(fresh)
    idless = {k: v for k, v in built.items() if v.get("unresolved")}
    crosswalk.update({k: v for k, v in built.items() if k not in idless})
    crosswalk.update(retired)
    if fresh or retired:
        # Id-less answers stay out, like timeouts, so the key is asked again next run.
        ledger.write_crosswalk({k: v for k, v in crosswalk.items() if k not in idless})
    # Merged after the write, never into it: persisting a timeout here would mean never
    # re-asking about that key, misreading the show as never-watched permanently.
    crosswalk.update(unreachable)
    crosswalk.update(idless)

    unattributed_keys = {k for k, v in crosswalk.items() if v.get("unresolved")}
    unattributed = [row for key in unattributed_keys for row in rows_by_key.get(key, [])]
    if unattributed:
        log(
            f"{len(unattributed)} play(s) across {len(unattributed_keys)} retired Plex items "
            "cannot be attributed by id; series sharing their titles go to review"
        )
    return crosswalk, unattributed


def count_genres(series: list[dict]) -> dict[str, int]:
    """How many series with episodes on disk the library holds per genre.

    "A subject the library demonstrably follows" is meant to be counted, not
    assumed, so the count has to come from the library rather than a guess.
    """
    counts: dict[str, int] = {}
    for record in series:
        if not int((record.get("statistics") or {}).get("episodeFileCount") or 0):
            continue
        for genre in record.get("genres") or []:
            counts[genre] = counts.get(genre, 0) + 1
    return counts


def assess_library(
    series: list[dict],
    tag_labels: dict[int, str],
    ctx: PlanContext,
    evidence: dict[str, Any],
    ledger: Ledger,
) -> list[Assessment]:
    """Run every series through the engine's rules."""
    first_seen = ledger.read_first_seen()
    bootstrapped: dict[int, datetime] = {}
    assessments: list[Assessment] = []
    feeds = evidence.get("feed_tags", DEFAULT_FEED_TAGS)

    for record in series:
        show = to_show(record, tag_labels)
        provenance = resolve_provenance(show, evidence["requests"], feeds)
        availability = resolve_availability(
            show,
            evidence["import_events"].get(show.series_id, []),
            evidence["horizon"],
            first_seen.get(show.series_id),
            ctx.now,
        )
        if availability.source == "ledger-bootstrap" and availability.first_playable:
            bootstrapped[show.series_id] = availability.first_playable
        viewing = resolve_viewing(
            show,
            evidence["rows_by_key"],
            evidence["crosswalk"],
            ctx.coverage_start,
            availability,
            history_ok=ctx.history_ok,
            unattributed_rows=evidence["unattributed"],
        )
        assessments.append(assess(show, provenance, availability, viewing, ctx))

    if bootstrapped:
        ledger.write_first_seen(bootstrapped)
    return assessments


def build_snapshot(
    series: list[dict],
    tag_labels: dict[int, str],
    exclusions: list[dict],
    history_meta: dict,
    history_ok: bool,
    now: datetime,
) -> dict[str, Any]:
    """The population figures anomaly detection compares between runs."""
    return {
        "library_size": len(series),
        "keep_tag_count": sum(1 for s in series if TAG_KEEP in {tag_labels.get(t) for t in s.get("tags", [])}),
        "exclusion_count": len(exclusions),
        "history_rows": history_meta.get("retrieved", 0),
        "history_ok": history_ok,
        "invalid_added_count": sum(1 for s in series if utc(s.get("added")) is None),
        "taken_at": now.isoformat(),
    }


def expired_basis_keeps(assessments: list[Assessment]) -> list[dict]:
    """Series carrying ``keep`` whose rating never justified it.

    Sonarr does not record *why* a tag was granted, so this reports the
    observation and makes no claim about when the decision was made.
    """
    return [
        {
            "series_id": a.show.series_id,
            "title": a.show.title,
            "year": a.show.year,
            "imdb_votes": a.show.imdb_votes,
            "imdb_score": a.show.imdb_score,
            "observation": (
                "carries keep, has aired, and has no rating or fewer than 1,000 votes; "
                "why the tag was granted is not recorded"
            ),
        }
        for a in assessments
        if TAG_KEEP in a.show.tags
        and a.show.status != "upcoming"
        and (a.show.imdb_votes is None or a.show.imdb_votes < 1000)
    ]


@dataclass
class Library:
    """One read of everything Sonarr and the request system can tell us."""

    tag_labels: dict[int, str]
    series: list[dict]
    exclusions: list[dict]
    horizon: datetime | None
    requests: dict[tuple[str, int], dict]
    # False only when a request system is configured and did not answer. An
    # estate with none configured is a deliberate choice, not an outage.
    requests_ok: bool = True


def gather_library(sources: Sources) -> Library:
    """Fetch the library and everything joined to it."""
    sonarr = sources.sonarr
    tag_labels = {t["id"]: t["label"] for t in sonarr.tags()}
    series = sonarr.all_series()
    exclusions = sonarr.exclusions()
    horizon = utc(sonarr.history_horizon())
    log(f"library {len(series)} series, {len(exclusions)} exclusions")
    log(f"Sonarr history reaches back to {horizon.date() if horizon else 'unknown'}")

    requests: dict[tuple[str, int], dict] = {}
    requests_ok = sources.seerr is None
    if sources.seerr:
        try:
            requests = sources.seerr.tv_requests()
            requests_ok = True
            log(f"request system: {len(requests)} TV request keys")
        except SourceError as exc:
            # Execution only re-reads requests made *since* the plan, so a request made
            # before this outage would never be seen again.
            log(f"request system unavailable: {exc}")
    return Library(tag_labels, series, exclusions, horizon, requests, requests_ok)


def build_plan_payload(
    run_id: str,
    now: datetime,
    dry_run: bool,
    ctx: PlanContext,
    assessments: list[Assessment],
    run: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the plan document handed to the model and stored in the ledger."""
    candidates = [a for a in assessments if a.outcome is Outcome.CANDIDATE]
    coverage_start = run["coverage_start"]
    horizon = run["horizon"]
    return {
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "mode": "dry-run" if dry_run else "act",
        "scope_start": ctx.scope_start.isoformat(),
        "counts": summarise(assessments),
        "snapshot": run["snapshot"],
        "blocks": run["blocks"],
        "recycle_bin": run["recycle_bin"],
        "history": run["history"],
        "history_coverage_start": (coverage_start.isoformat() if coverage_start else None),
        "unattributed_plays": run["unattributed"],
        "sonarr_history_horizon": horizon.isoformat() if horizon else None,
        "keep_tag_additions": [
            {
                "series_id": s.series_id,
                "title": s.title,
                "imdb_score": s.imdb_score,
                "imdb_votes": s.imdb_votes,
            }
            for s in run["keep_targets"]
        ],
        "candidates": [a.to_json() for a in candidates],
        "review": [a.to_json() for a in assessments if a.outcome is Outcome.REVIEW],
        "missing_file": [a.to_json() for a in assessments if a.outcome is Outcome.MISSING_FILE],
        "multi_user_viewers": [a.to_json() for a in assessments if a.viewing.distinct_viewers >= 2],
        "expired_basis_keeps": expired_basis_keeps(assessments),
    }


def source_blocks(
    bin_ok: bool,
    bin_detail: str,
    history_ok: bool,
    history_note: str,
    requests_ok: bool,
) -> list[str]:
    """Reasons this run must not act, drawn from the state of its sources.

    Each one is a check that could not be performed rather than a check that
    failed, and the difference decides everything: an unavailable safeguard is
    an unresolved safeguard.
    """
    blocks = []
    if not bin_ok:
        blocks.append(bin_detail)
    if not history_ok:
        blocks.append(f"play history not usable: {history_note}")
    if not requests_ok:
        # An empty request set reads like nobody asked; for a feed-tagged show that
        # gap is the whole authorisation to delete it.
        blocks.append("request system unreachable: a requested show could not be told from a feed pull")
    return blocks


def cmd_plan(args: argparse.Namespace) -> int:
    """Assess the whole library and emit the candidate set for judgement."""
    sources = build_sources()
    sonarr, ledger = sources.sonarr, sources.ledger
    now = datetime.now(timezone.utc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    log(f"run {sources.run_id} (mode={'DRY RUN' if ledger.dry_run else 'ACT'})")
    log(f"Sonarr {sonarr.status().get('version')} reachable")
    bin_ok, bin_detail = recycle_bin_ready(sonarr.media_management())
    log(f"recycle bin: {bin_detail}")

    rows, history_meta, history_ok, history_note = fetch_history(sources.tautulli)
    library = gather_library(sources)

    rows_by_key, unkeyed = group_by_show(rows)
    crosswalk, unattributed = resolve_identities(sources.tautulli, ledger, rows_by_key)
    unattributed = unattributed + unkeyed

    stamps = [int(r["date"]) for r in rows if str(r.get("date", "")).isdigit()]
    coverage_start = datetime.fromtimestamp(min(stamps), tz=timezone.utc) if stamps else None
    log(f"play history covers from {coverage_start.date() if coverage_start else 'unknown'}")

    ctx = PlanContext(
        now=now,
        scope_start=utc(os.environ.get("CLEANUP_SCOPE_START") or DEFAULT_SCOPE_START) or now,
        coverage_start=coverage_start,
        history_ok=history_ok,
        decisions=ledger.read_decisions(),
        genre_counts=count_genres(library.series),
    )
    assessments = assess_library(
        library.series,
        library.tag_labels,
        ctx,
        {
            "requests": library.requests,
            "feed_tags": feed_tags(),
            "import_events": sonarr.import_events(),
            "horizon": library.horizon,
            "rows_by_key": rows_by_key,
            "crosswalk": crosswalk,
            "unattributed": unattributed,
        },
        ledger,
    )
    counts = summarise(assessments)
    log(f"assessment: {counts}")

    snapshot = build_snapshot(library.series, library.tag_labels, library.exclusions, history_meta, history_ok, now)
    blocks = check_anomalies(snapshot, ledger.read_baseline()) + source_blocks(
        bin_ok, bin_detail, history_ok, history_note, library.requests_ok
    )

    problems = snapshot_valid(snapshot)
    if problems:
        log(f"snapshot rejected as a baseline: {problems}")
    elif not blocks or ledger.read_baseline() is None:
        ledger.write_baseline(snapshot)
        log("baseline recorded")

    keep_targets = keep_tag_targets([a.show for a in assessments])
    log(f"{len(keep_targets)} series qualify for the keep tag and do not carry it")

    payload = build_plan_payload(
        sources.run_id,
        now,
        ledger.dry_run,
        ctx,
        assessments,
        {
            "snapshot": snapshot,
            "blocks": blocks,
            "recycle_bin": bin_detail,
            "history": history_meta,
            "coverage_start": coverage_start,
            "unattributed": len(unattributed),
            "horizon": library.horizon,
            "keep_targets": keep_targets,
        },
    )
    (out / "plan.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ledger.record_plan({k: v for k, v in payload.items() if k != "review"})
    log(f"wrote {out / 'plan.json'}: {len(payload['candidates'])} candidates, " f"{len(blocks)} blocking issues")
    for block in blocks:
        log(f"  BLOCK: {block}")
    return 0


def rehydrate(planned: dict, show: Show, reason: str = "") -> Assessment:
    """Rebuild an assessment from a plan entry plus the live series record.

    Only the evidence needed for execution is restored; the protections are
    re-derived from live state rather than trusted from the plan.
    """
    return Assessment(
        show=show,
        outcome=Outcome.CANDIDATE,
        subject_counts=dict(planned.get("subject_counts") or {}),
        reopened_dismissal=planned.get("reopened_dismissal") is True,
        provenance=Provenance(Origin(planned["origin"]), tuple(planned.get("origin_evidence") or ())),
        availability=Availability(None, planned["availability_source"], show.has_file),
        viewing=Viewing(
            status=HistoryStatus(planned["history_status"]),
            identity_via=planned.get("identity_via", "unresolved"),
            rating_keys=tuple(planned.get("rating_keys") or ()),
            # Carried through: the fingerprint counts viewers, and dropping them would
            # record a spare against viewers=0, stale the day it's made.
            progress=tuple(
                EpisodeProgress(user_id=int(v["user_id"]), episodes=int(v["episodes"]))
                for v in (planned.get("viewers") or [])
            ),
            play_count=int(planned.get("play_count") or 0),
        ),
        reasons=[reason] if reason else [],
    )


def apply_keep_tags(  # pylint: disable=too-many-arguments
    sonarr: Sonarr,
    additions: list[dict],
    dry_run: bool,
    live: dict[int, dict] | None = None,
    tag_labels: dict[int, str] | None = None,
    tag_in_dry_run: bool = False,
) -> dict[str, Any]:
    """Give the permanent ``keep`` tag to series that have earned it.

    Add-only. The editor endpoint answers 202, meaning queued and nothing more,
    so the count is confirmed by re-reading rather than assumed.

    Unlike the film engine this may write in a dry run, when ``tag_in_dry_run``
    is set: Reclaimerr protects anything tagged ``keep``, and an add-only tag is
    reversible, whereas a dry run that writes nothing leaves Reclaimerr blind.

    The plan is half an hour old by the time this runs, so each target is
    re-checked against its live tags first: applying ``keep`` to a series somebody
    has just marked ``cleanup-eligible`` would reverse that person's decision.
    """
    series_ids = [a["series_id"] for a in additions if isinstance(a.get("series_id"), int)]
    withdrawn = 0
    if live is not None:
        kept = []
        for series_id in series_ids:
            record = live.get(series_id)
            if record is None or not keep_tag_applies(to_show(record, tag_labels or {})):
                withdrawn += 1
                continue
            kept.append(series_id)
        series_ids = kept
    if not series_ids:
        return {"requested": 0, "applied": 0, "withdrawn": withdrawn, "note": "nothing qualified"}
    if dry_run and not tag_in_dry_run:
        return {
            "requested": len(series_ids),
            "applied": 0,
            "withdrawn": withdrawn,
            "note": "dry run: not applied",
        }

    tags = {t["label"]: t["id"] for t in sonarr.tags()}
    if TAG_KEEP not in tags:
        sonarr.create_tag(TAG_KEEP)
        tags = {t["label"]: t["id"] for t in sonarr.tags()}
    tag_id = tags[TAG_KEEP]

    status, _ = sonarr.add_tag(series_ids, tag_id)
    # Confirmed per series by re-reading, whatever the status said: a keep counted
    # as applied when it never landed is recorded as settled and never retried.
    wanted = set(series_ids)
    confirmed = sorted(s["id"] for s in sonarr.all_series() if s["id"] in wanted and tag_id in s.get("tags", []))
    unconfirmed = sorted(wanted - set(confirmed))
    note = "applied in a dry run (TAG_KEEPS_IN_DRY_RUN)" if dry_run else "applied"
    if unconfirmed:
        note = f"HTTP {status}; {len(unconfirmed)} not confirmed on re-read"
    return {
        "requested": len(series_ids),
        "applied": len(confirmed),
        "withdrawn": withdrawn,
        "http": status,
        "note": note,
        "unconfirmed": unconfirmed,
    }


def plan_refusal(plan: dict, run_mode: str, now: datetime) -> str | None:
    """Why this plan must not be acted on, or None if it may be.

    Its mode must match this run's: the ledger namespaces dry-run state under
    its own prefix, so planning in one mode and executing in the other reads
    rehearsal baselines and decisions while writing real ones.

    And it must be recent. Live re-validation refreshes tags, status and current
    playback -- but not play history or requests. A plan left behind by a failed
    earlier schedule describes a library that has moved on.
    """
    plan_mode = plan.get("mode")
    if plan_mode != run_mode:
        return f"plan was made in {plan_mode!r} but this run is {run_mode!r}"

    generated = utc(plan.get("generated_at"))
    if generated is None:
        return "plan has no usable generated_at"

    max_age = float(os.environ.get("CLEANUP_MAX_PLAN_AGE_HOURS", "6"))
    age_hours = (now - generated).total_seconds() / 3600
    if age_hours > max_age:
        return f"plan is {age_hours:.1f}h old, over the {max_age}h limit"
    return None


def record_judge_decisions(
    accepted: list[dict],
    series: dict[int, dict],
    by_id: dict[int, dict],
    tag_labels: dict[int, str],
    ledger: Ledger,
    run_id: str,
    unconfirmed: set[int] | frozenset[int] = frozenset(),
) -> list[dict]:
    """Persist what the judge decided, and collect what it sent to a person.

    A keep whose tag could not be confirmed is not recorded, so next week judges
    and tags it again rather than treating it as settled.

    Returns the escalations, because the plan's review list was written before
    judgement: without carrying them out of here, a series the judge deliberately
    put in front of somebody would wait until next week's plan to be seen.
    """
    now = datetime.now(timezone.utc)
    escalated: list[dict] = []
    for item in accepted:
        if item["verdict"] not in ("keep", "review") or item["series_id"] not in series:
            continue
        if item["verdict"] == "keep" and item["series_id"] in unconfirmed:
            continue
        show = to_show(series[item["series_id"]], tag_labels)
        if item["verdict"] == "review":
            escalated.append(
                {
                    "series_id": show.series_id,
                    "title": show.title,
                    "year": show.year,
                    "size_gb": show.size_gb,
                    "reason": item["reason"],
                }
            )
        ledger.write_decision(
            build_decision_record(
                rehydrate(by_id[item["series_id"]], show),
                verdict=("spared" if item["verdict"] == "keep" else "queued for review"),
                reason=item["reason"],
                run_id=run_id,
                now=now,
            )
        )
    return escalated


def load_recommendations(args: argparse.Namespace, plan: dict) -> list | int:
    """The recommendations for this plan, or the exit code refusing them."""
    recommendations = json.loads(Path(args.recommendations).read_text(encoding="utf-8"))
    if isinstance(recommendations, dict):
        # Both files outlive the run on the same volume; without this check a judging
        # step that died leaves last week's verdicts to act on this week's candidates.
        if recommendations.get("plan_run_id") != plan.get("run_id"):
            log(
                "refusing to execute: recommendations were judged against plan "
                f"{recommendations.get('plan_run_id')!r}, not {plan.get('run_id')!r}"
            )
            return 7
        recommendations = recommendations.get("recommendations", [])
    return recommendations


def execute_exit_code(result: ExecutionResult, untagged: set[int], unjudged: list[int], rejected: list[dict]) -> int:
    """The exit code for a finished execute run; the README lists each one."""
    # A failed deletion is a safe no-op to retry; an uncertain one is recorded and left,
    # since a retry could double up on a DELETE that already landed (docs/docs/apps/curator.md).
    if result.uncertain:
        log(
            f"exiting non-zero: {len(result.uncertain)} deletion(s) could not be "
            "confirmed; the next run reconciles them against live state"
        )
        return 4
    if result.failed:
        log("exiting non-zero: some deletions failed outright")
        return 4
    unavailable = sum(1 for item in result.skipped if item.get("unavailable"))
    if unavailable:
        log(f"exiting non-zero: {unavailable} skip(s) because a safety check could not be read")
        return 10
    if untagged:
        log(f"exiting non-zero: {len(untagged)} keep tag(s) not confirmed; they are re-judged next run")
        return 8
    # A week nobody judged must not count as a success, or it resets the staleness alert.
    if unjudged or rejected:
        log(f"exiting non-zero: {len(unjudged)} candidate(s) unjudged, {len(rejected)} verdict(s) rejected")
        return 9
    return 0


def cmd_execute(args: argparse.Namespace) -> int:  # pylint: disable=too-many-locals
    """Apply Claude's recommendations, re-validating every safeguard first."""
    sources = build_sources()
    sonarr, ledger = sources.sonarr, sources.ledger
    # The CronJob passes --read-only, so deleting needs edits in two places; a half-made
    # change then refuses loudly rather than deleting quietly (README: read-only runs).
    if getattr(args, "read_only", False) and not ledger.dry_run:
        log("refusing to execute: --read-only was passed but CLEANUP_MODE is act")
        return 5
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    recommendations = load_recommendations(args, plan)
    if isinstance(recommendations, int):
        return recommendations

    if plan.get("blocks"):
        log("refusing to execute: the plan recorded blocking issues")
        for block in plan["blocks"]:
            log(f"  BLOCK: {block}")
        return 2

    refusal = plan_refusal(plan, "dry-run" if ledger.dry_run else "act", datetime.now(timezone.utc))
    if refusal:
        log(f"refusing to execute: {refusal}")
        return 5

    tag_labels = {t["id"]: t["label"] for t in sonarr.tags()}
    by_id = {item["series_id"]: item for item in plan.get("candidates", [])}

    accepted, rejected = validate_recommendations(recommendations, by_id)
    for bad in rejected:
        log(f"  rejected recommendation: {bad}")
    # A candidate the judge left out is safe but reaches nobody: neither deleted nor
    # put in front of a person. Counting it is what makes that visible.
    unjudged = sorted(set(by_id) - {item["series_id"] for item in accepted})
    if unjudged:
        log(f"  {len(unjudged)} candidate(s) came back with no usable verdict")

    dangling = ledger.unreconciled_intents()
    if dangling:
        log(f"reconciling {len(dangling)} interrupted deletions from earlier runs")
        for entry in reconcile(dangling, sonarr, ledger):
            log(f"  {entry}")

    acquired, why = ledger.acquire_lock()
    if not acquired:
        log(f"refusing to execute: {why}")
        return 3

    try:
        series = {s["id"]: s for s in sonarr.all_series()}
        approved = [
            rehydrate(by_id[item["series_id"]], to_show(series[item["series_id"]], tag_labels), item["reason"])
            for item in accepted
            if item["verdict"] == "delete" and item["series_id"] in series
        ]
        in_dry_run = tag_keeps_in_dry_run()
        # Applied before any deletion: catches a plan built before this protection rule
        # from deleting a series assessment would now protect.
        tagged = apply_keep_tags(
            sonarr,
            plan.get("keep_tag_additions") or [],
            ledger.dry_run,
            live=series,
            tag_labels=tag_labels,
            tag_in_dry_run=in_dry_run,
        )
        # Bar targets are PROTECTED and never candidates, so the two sets cannot overlap.
        judge_tagged = apply_keep_tags(
            sonarr,
            [{"series_id": i["series_id"]} for i in accepted if i["verdict"] == "keep"],
            ledger.dry_run,
            live=series,
            tag_labels=tag_labels,
            tag_in_dry_run=in_dry_run,
        )

        already = ledger.deletions_in_window()
        selected, deferred = select_batch(approved, MAX_DELETIONS_PER_RUN, already)
        log(
            f"{len(approved)} approved, {already} already deleted this window, "
            f"acting on {len(selected)}, deferring {len(deferred)}"
        )

        result = execute(
            selected,
            sonarr,
            sources.tautulli,
            ledger,
            tag_labels,
            dry_run=ledger.dry_run,
            seerr=sources.seerr,
            crosswalk=ledger.read_crosswalk(),
            planned_at=utc(plan.get("generated_at")),
            planned_files={
                sid: int(item["episode_file_count"])
                for sid, item in by_id.items()
                if isinstance(item.get("episode_file_count"), int)
            },
        )

        untagged = set(tagged.get("unconfirmed") or []) | set(judge_tagged.get("unconfirmed") or [])
        escalated = record_judge_decisions(
            accepted, series, by_id, tag_labels, ledger, sources.run_id, unconfirmed=untagged
        )
        summary = {
            "run_id": sources.run_id,
            # Separate CronJobs generate their own run ids; this ties the result back to
            # the plan, so the digest can tell this week's result from an older one.
            "plan_run_id": plan.get("run_id"),
            "unjudged": unjudged,
            "mode": "dry-run" if ledger.dry_run else "act",
            "escalated": escalated,
            "keep_tags_added": tagged,
            "judge_keeps_tagged": judge_tagged,
            "deferred": [a.show.series_id for a in deferred],
            "rejected_recommendations": rejected,
            **result.as_dict(),
        }
        Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log(
            f"deleted={len(result.deleted)} skipped={len(result.skipped)} "
            f"failed={len(result.failed)} uncertain={len(result.uncertain)} "
            f"keep-tagged={tagged.get('applied', 0)}+{judge_tagged.get('applied', 0)}"
        )
        return execute_exit_code(result, untagged, unjudged, rejected)
    finally:
        ledger.release_lock()


def cmd_report(args: argparse.Namespace) -> int:
    """Render the run's digest and deliver it."""
    out = Path(args.out)
    digest = reporting.render(
        reporting.read(Path(args.plan)),
        reporting.read(Path(args.result)),
        reporting.read(Path(args.judgement)) if args.judgement else None,
        datetime.now(timezone.utc),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(digest.text, encoding="utf-8")
    print(digest.text, flush=True)

    url = env("CLEANUP_WEBHOOK_URL", required=False)
    if not url:
        log("no CLEANUP_WEBHOOK_URL: digest written but not delivered")
        return 0
    try:
        log(reporting.post(url, env("CLEANUP_WEBHOOK_TOKEN", required=False), digest))
    except reporting.NotificationError as exc:
        # Loud on purpose: an undelivered digest looks, from the phone, like a week
        # the job never ran at all.
        log(f"DIGEST NOT DELIVERED: {exc}")
        return 6
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Run the fixture test suite."""
    suite = unittest.TestLoader().discover(
        str(Path(__file__).parent / "tests"),
        top_level_dir=str(Path(__file__).parent.parent),
    )
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(prog="curatortv", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="assess the library; writes no changes")
    plan.add_argument("--out", default="./cleanup-plan")
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("execute", help="apply recommendations against a plan")
    run.add_argument("--plan", required=True)
    run.add_argument("--recommendations", required=True)
    run.add_argument("--out", default="./cleanup-result.json")
    run.add_argument(
        "--read-only",
        action="store_true",
        help="refuse to delete even if CLEANUP_MODE says otherwise",
    )
    run.set_defaults(func=cmd_execute)

    digest = sub.add_parser("report", help="render and deliver the run digest")
    digest.add_argument("--plan", required=True)
    digest.add_argument("--result", required=True)
    digest.add_argument("--judgement", default="")
    digest.add_argument("--out", default="./cleanup-report.md")
    digest.set_defaults(func=cmd_report)

    test = sub.add_parser("selftest", help="run the fixture tests")
    test.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
