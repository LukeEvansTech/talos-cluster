"""Command-line entry point: ``plan``, ``execute`` and ``selftest``.

``plan`` is read-only and always safe to run. ``execute`` is the only command that
can change anything, and only when ``CLEANUP_MODE=act``.
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

from .clients import Radarr, RequestSystem, SourceError, Tautulli
from .evidence import (
    build_crosswalk,
    resolve_availability,
    resolve_provenance,
    resolve_viewing,
)
from .executor import execute, reconcile, recycle_bin_ready, validate_recommendations
from .ledger import Ledger
from .model import (
    TAG_KEEP,
    Assessment,
    Availability,
    Completion,
    Film,
    HistoryStatus,
    Origin,
    Outcome,
    Provenance,
    Viewing,
    utc,
)
from .planner import (
    MAX_DELETIONS_PER_RUN,
    PlanContext,
    assess,
    build_decision_record,
    check_anomalies,
    keep_tag_targets,
    select_batch,
    snapshot_valid,
    summarise,
)
from .s3 import S3Client, S3Config

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


def to_film(record: dict, tag_labels: dict[int, str]) -> Film:
    """Project a Radarr movie record onto the fields decisions depend on."""
    ratings = (record.get("ratings") or {}).get("imdb") or {}
    votes = ratings.get("votes")
    value = ratings.get("value")
    return Film(
        movie_id=record["id"],
        tmdb_id=record.get("tmdbId"),
        imdb_id=record.get("imdbId"),
        title=record.get("title", ""),
        year=record.get("year"),
        status=record.get("status", ""),
        has_file=bool(record.get("hasFile")),
        size_bytes=int(record.get("sizeOnDisk") or 0),
        added=utc(record.get("added")),
        tags=frozenset(tag_labels.get(t, str(t)) for t in record.get("tags", [])),
        imdb_score=float(value) if isinstance(value, (int, float)) and value else None,
        imdb_votes=int(votes) if isinstance(votes, int) and votes else None,
        collection_title=(record.get("collection") or {}).get("title"),
        genres=tuple(record.get("genres") or ()),
        studio=record.get("studio"),
        original_language=(record.get("originalLanguage") or {}).get("name"),
        overview=record.get("overview") or "",
        file_date_added=utc((record.get("movieFile") or {}).get("dateAdded")),
    )


@dataclass
class Sources:
    """Every client one run needs, plus its identity."""

    radarr: Radarr
    tautulli: Tautulli
    seerr: RequestSystem | None
    ledger: Ledger
    run_id: str


def build_sources() -> Sources:
    """Construct every client from the environment."""
    run_id = os.environ.get("CLEANUP_RUN_ID") or (
        f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    )
    seerr_url = env("SEERR_URL", required=False)
    return Sources(
        radarr=Radarr(env("RADARR_URL"), env("RADARR_API_KEY")),
        tautulli=Tautulli(env("TAUTULLI_URL"), env("TAUTULLI_API_KEY")),
        seerr=(RequestSystem(seerr_url, env("SEERR_API_KEY", required=False)) if seerr_url else None),
        ledger=Ledger(
            S3Client(
                S3Config(
                    endpoint=env("LEDGER_ENDPOINT"),
                    access_key=env("LEDGER_ACCESS_KEY_ID"),
                    secret_key=env("LEDGER_SECRET_ACCESS_KEY"),
                    bucket=env("LEDGER_BUCKET"),
                )
            ),
            run_id=run_id,
            dry_run=os.environ.get("CLEANUP_MODE", "dry-run").lower() != "act",
        ),
        run_id=run_id,
    )


def monitored_collection_ids(collections: list[dict]) -> set[int]:
    """Every tmdb id inside a collection Radarr has been told to complete."""
    return {
        entry["tmdbId"]
        for collection in collections
        if collection.get("monitored")
        for entry in (collection.get("movies") or [])
        if entry.get("tmdbId")
    }


def fetch_history(tautulli: Tautulli) -> tuple[list[dict], dict, bool, str]:
    """Play history, with an honest account of whether it can be trusted."""
    try:
        tautulli.preflight()
        rows, meta = tautulli.movie_history()
    except SourceError as exc:
        log(f"play history UNAVAILABLE: {exc}")
        return (
            [],
            {"declared": None, "retrieved": 0, "complete": False},
            False,
            str(exc),
        )

    log(f"play history: {meta['retrieved']} rows (declared {meta['declared']})")
    if not meta["complete"]:
        return rows, meta, False, "retrieved fewer rows than the service declared"
    return rows, meta, True, ""


def resolve_identities(
    tautulli: Tautulli, ledger: Ledger, rows_by_key: dict[int, list[dict]]
) -> tuple[dict[int, dict], list[dict]]:
    """Map Plex rating keys to external ids, remembering the ones that cannot be.

    Plex reissues a rating key when an item is replaced and ``get_metadata`` 404s
    on the old one, but the plays behind it were still real plays. Those are kept
    and matched back by title, so they taint a zero rather than vanishing from it.
    """
    crosswalk = ledger.read_crosswalk()
    missing = [k for k in rows_by_key if k not in crosswalk]
    log(f"resolving {len(missing)} new Plex rating keys ({len(crosswalk)} cached)")

    fresh: dict[int, dict] = {}
    retired: dict[int, dict] = {}
    for key in missing:
        meta = tautulli.metadata(key)
        if meta:
            fresh[key] = meta
        else:
            sample = rows_by_key[key][0]
            retired[key] = {
                "tmdb": None,
                "imdb": None,
                "title": sample.get("title") or sample.get("full_title"),
                "year": sample.get("year"),
                "unresolved": True,
            }
    if fresh:
        crosswalk.update(build_crosswalk(fresh))
    crosswalk.update(retired)
    if fresh or retired:
        ledger.write_crosswalk(crosswalk)

    unattributed_keys = {k for k, v in crosswalk.items() if v.get("unresolved")}
    unattributed = [row for key in unattributed_keys for row in rows_by_key.get(key, [])]
    if unattributed:
        log(
            f"{len(unattributed)} play(s) across {len(unattributed_keys)} retired Plex items "
            "cannot be attributed by id; films sharing their titles go to review"
        )
    return crosswalk, unattributed


def assess_library(
    movies: list[dict],
    tag_labels: dict[int, str],
    ctx: PlanContext,
    evidence: dict[str, Any],
    ledger: Ledger,
) -> list[Assessment]:
    """Run every film through the engine's rules."""
    first_seen = ledger.read_first_seen()
    bootstrapped: dict[int, datetime] = {}
    assessments: list[Assessment] = []

    for record in movies:
        film = to_film(record, tag_labels)
        provenance = resolve_provenance(film, evidence["requests"])
        availability = resolve_availability(
            film,
            evidence["import_events"].get(film.movie_id, []),
            evidence["horizon"],
            first_seen.get(film.movie_id),
            ctx.now,
        )
        if availability.source == "ledger-bootstrap" and availability.first_playable:
            bootstrapped[film.movie_id] = availability.first_playable
        viewing = resolve_viewing(
            film,
            evidence["rows_by_key"],
            evidence["crosswalk"],
            ctx.coverage_start,
            availability,
            history_ok=ctx.history_ok,
            unattributed_rows=evidence["unattributed"],
        )
        assessments.append(assess(film, provenance, availability, viewing, ctx))

    if bootstrapped:
        ledger.write_first_seen(bootstrapped)
    return assessments


def build_snapshot(
    movies: list[dict],
    tag_labels: dict[int, str],
    collections: list[dict],
    exclusions: list[dict],
    history_meta: dict,
    history_ok: bool,
    now: datetime,
) -> dict[str, Any]:
    """The population figures anomaly detection compares between runs."""
    return {
        "library_size": len(movies),
        "keep_tag_count": sum(1 for m in movies if TAG_KEEP in {tag_labels.get(t) for t in m.get("tags", [])}),
        "collection_count": len(collections),
        "monitored_collections": sum(1 for c in collections if c.get("monitored")),
        "exclusion_count": len(exclusions),
        "history_rows": history_meta.get("retrieved", 0),
        "history_ok": history_ok,
        "collections_loaded": bool(collections),
        "invalid_added_count": sum(1 for m in movies if utc(m.get("added")) is None),
        "taken_at": now.isoformat(),
    }


def expired_basis_keeps(assessments: list[Assessment]) -> list[dict]:
    """Films carrying ``keep`` whose rating never justified it.

    Radarr does not record *why* a tag was granted, so this reports the
    observation and makes no claim about when the decision was made.
    """
    return [
        {
            "movie_id": a.film.movie_id,
            "title": a.film.title,
            "year": a.film.year,
            "imdb_votes": a.film.imdb_votes,
            "imdb_score": a.film.imdb_score,
            "observation": (
                "carries keep, is released, and has no rating or fewer than 1,000 votes; "
                "why the tag was granted is not recorded"
            ),
        }
        for a in assessments
        if TAG_KEEP in a.film.tags
        and a.film.status == "released"
        and (a.film.imdb_votes is None or a.film.imdb_votes < 1000)
    ]


@dataclass
class Library:
    """One read of everything Radarr and the request system can tell us."""

    tag_labels: dict[int, str]
    movies: list[dict]
    collections: list[dict]
    exclusions: list[dict]
    horizon: datetime | None
    requests: dict[int, dict]


def gather_library(sources: Sources) -> Library:
    """Fetch the library and everything joined to it."""
    radarr = sources.radarr
    tag_labels = {t["id"]: t["label"] for t in radarr.tags()}
    movies = radarr.movies()
    collections = radarr.collections()
    exclusions = radarr.exclusions()
    horizon = utc(radarr.history_horizon())
    log(f"library {len(movies)} films, {len(collections)} collections, {len(exclusions)} exclusions")
    log(f"Radarr history reaches back to {horizon.date() if horizon else 'unknown'}")

    requests: dict[int, dict] = {}
    if sources.seerr:
        try:
            requests = sources.seerr.movie_requests()
            log(f"request system: {len(requests)} movie requests")
        except SourceError as exc:
            log(f"request system unavailable: {exc} -- every origin will read as unknown")

    monitored = monitored_collection_ids(collections)
    log(
        f"{sum(1 for c in collections if c.get('monitored'))} monitored collections "
        f"protect {len(monitored)} tmdb ids"
    )
    return Library(tag_labels, movies, collections, exclusions, horizon, requests)


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
        "radarr_history_horizon": horizon.isoformat() if horizon else None,
        "keep_tag_additions": [
            {
                "movie_id": f.movie_id,
                "title": f.title,
                "imdb_score": f.imdb_score,
                "imdb_votes": f.imdb_votes,
            }
            for f in run["keep_targets"]
        ],
        "candidates": [a.to_json() for a in candidates],
        "review": [a.to_json() for a in assessments if a.outcome is Outcome.REVIEW],
        "missing_file": [a.to_json() for a in assessments if a.outcome is Outcome.MISSING_FILE],
        "multi_user_completions": [a.to_json() for a in assessments if a.viewing.distinct_completers >= 2],
        "expired_basis_keeps": expired_basis_keeps(assessments),
    }


def cmd_plan(args: argparse.Namespace) -> int:
    """Assess the whole library and emit the candidate set for judgement."""
    sources = build_sources()
    radarr, ledger = sources.radarr, sources.ledger
    now = datetime.now(timezone.utc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    log(f"run {sources.run_id} (mode={'DRY RUN' if ledger.dry_run else 'ACT'})")
    log(f"Radarr {radarr.status().get('version')} reachable")
    bin_ok, bin_detail = recycle_bin_ready(radarr.media_management())
    log(f"recycle bin: {bin_detail}")

    rows, history_meta, history_ok, history_note = fetch_history(sources.tautulli)

    library = gather_library(sources)
    tag_labels, movies, collections = (
        library.tag_labels,
        library.movies,
        library.collections,
    )
    exclusions, horizon = library.exclusions, library.horizon
    monitored = monitored_collection_ids(collections)

    rows_by_key: dict[int, list[dict]] = {}
    for row in rows:
        key = row.get("rating_key")
        if key is None or key == "":
            continue
        rows_by_key.setdefault(int(key), []).append(row)
    crosswalk, unattributed = resolve_identities(sources.tautulli, ledger, rows_by_key)

    stamps = [int(r["date"]) for r in rows if str(r.get("date", "")).isdigit()]
    coverage_start = datetime.fromtimestamp(min(stamps), tz=timezone.utc) if stamps else None
    log(f"play history covers from {coverage_start.date() if coverage_start else 'unknown'}")

    ctx = PlanContext(
        now=now,
        scope_start=utc(os.environ.get("CLEANUP_SCOPE_START") or DEFAULT_SCOPE_START) or now,
        monitored_collection_tmdb_ids=frozenset(monitored),
        coverage_start=coverage_start,
        history_ok=history_ok,
        decisions=ledger.read_decisions(),
        collections_loaded=bool(collections),
    )
    assessments = assess_library(
        movies,
        tag_labels,
        ctx,
        {
            "requests": library.requests,
            "import_events": radarr.import_events(),
            "horizon": horizon,
            "rows_by_key": rows_by_key,
            "crosswalk": crosswalk,
            "unattributed": unattributed,
        },
        ledger,
    )
    counts = summarise(assessments)
    log(f"assessment: {counts}")

    snapshot = build_snapshot(movies, tag_labels, collections, exclusions, history_meta, history_ok, now)
    blocks = check_anomalies(snapshot, ledger.read_baseline())
    if not bin_ok:
        blocks.append(bin_detail)
    if not history_ok:
        blocks.append(f"play history not usable: {history_note}")

    problems = snapshot_valid(snapshot)
    if problems:
        log(f"snapshot rejected as a baseline: {problems}")
    elif not blocks or ledger.read_baseline() is None:
        ledger.write_baseline(snapshot)
        log("baseline recorded")

    keep_targets = keep_tag_targets([a.film for a in assessments])
    log(f"{len(keep_targets)} films qualify for the keep tag and do not carry it")

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


def rehydrate(planned: dict, film: Film, reason: str = "") -> Assessment:
    """Rebuild an assessment from a plan entry plus the live movie record.

    Only the evidence needed for execution is restored; the protections are
    re-derived from live state rather than trusted from the plan.
    """
    return Assessment(
        film=film,
        outcome=Outcome.CANDIDATE,
        provenance=Provenance(Origin(planned["origin"]), tuple(planned.get("origin_evidence") or ())),
        availability=Availability(None, planned["availability_source"], film.has_file),
        viewing=Viewing(
            status=HistoryStatus(planned["history_status"]),
            identity_via=planned.get("identity_via", "unresolved"),
            rating_keys=tuple(planned.get("rating_keys") or ()),
            # Carried through because the decision fingerprint counts completers.
            # Dropped, a spare is recorded against completers=0 while the next
            # plan computes the real number -- stale on the day it was made.
            completions=tuple(
                Completion(user_id=int(c["user_id"]), percent=int(c["percent"]))
                for c in (planned.get("completions") or [])
            ),
            play_count=int(planned.get("play_count") or 0),
        ),
        reasons=[reason] if reason else [],
    )


def apply_keep_tags(radarr: Radarr, additions: list[dict], dry_run: bool) -> dict[str, Any]:
    """Give the permanent allow-list tag to films that have earned it.

    Add-only. The editor endpoint answers 202, meaning queued and nothing more,
    so the count is confirmed by re-reading rather than assumed.
    """
    movie_ids = [a["movie_id"] for a in additions if isinstance(a.get("movie_id"), int)]
    if not movie_ids:
        return {"requested": 0, "applied": 0, "note": "nothing qualified"}
    if dry_run:
        return {
            "requested": len(movie_ids),
            "applied": 0,
            "note": "dry run: not applied",
        }

    tags = {t["label"]: t["id"] for t in radarr.tags()}
    if TAG_KEEP not in tags:
        radarr.create_tag(TAG_KEEP)
        tags = {t["label"]: t["id"] for t in radarr.tags()}
    tag_id = tags[TAG_KEEP]

    status, _ = radarr.add_tag(movie_ids, tag_id)
    wanted = set(movie_ids)
    applied = sum(1 for m in radarr.movies() if m["id"] in wanted and tag_id in m.get("tags", []))
    return {"requested": len(movie_ids), "applied": applied, "http": status}


def cmd_execute(args: argparse.Namespace) -> int:
    """Apply Claude's recommendations, re-validating every safeguard first."""
    sources = build_sources()
    radarr, ledger = sources.radarr, sources.ledger
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    recommendations = json.loads(Path(args.recommendations).read_text(encoding="utf-8"))
    if isinstance(recommendations, dict):
        recommendations = recommendations.get("recommendations", [])

    if plan.get("blocks"):
        log("refusing to execute: the plan recorded blocking issues")
        for block in plan["blocks"]:
            log(f"  BLOCK: {block}")
        return 2

    tag_labels = {t["id"]: t["label"] for t in radarr.tags()}
    monitored = monitored_collection_ids(radarr.collections())
    by_id = {item["movie_id"]: item for item in plan.get("candidates", [])}

    accepted, rejected = validate_recommendations(recommendations, by_id)
    for bad in rejected:
        log(f"  rejected recommendation: {bad}")

    dangling = ledger.unreconciled_intents()
    if dangling:
        log(f"reconciling {len(dangling)} interrupted deletions from earlier runs")
        for entry in reconcile(dangling, radarr, ledger):
            log(f"  {entry}")

    acquired, why = ledger.acquire_lock()
    if not acquired:
        log(f"refusing to execute: {why}")
        return 3

    try:
        movies = {m["id"]: m for m in radarr.movies()}
        approved = [
            rehydrate(
                by_id[item["movie_id"]],
                to_film(movies[item["movie_id"]], tag_labels),
                item["reason"],
            )
            for item in accepted
            if item["verdict"] == "delete" and item["movie_id"] in movies
        ]
        already = ledger.deletions_in_window()
        selected, deferred = select_batch(approved, MAX_DELETIONS_PER_RUN, already)
        log(
            f"{len(approved)} approved, {already} already deleted this window, "
            f"acting on {len(selected)}, deferring {len(deferred)}"
        )

        result = execute(
            selected,
            radarr,
            sources.tautulli,
            ledger,
            tag_labels,
            monitored,
            dry_run=ledger.dry_run,
        )
        tagged = apply_keep_tags(radarr, plan.get("keep_tag_additions") or [], ledger.dry_run)

        now = datetime.now(timezone.utc)
        for item in accepted:
            if item["verdict"] not in ("keep", "review") or item["movie_id"] not in movies:
                continue
            film = to_film(movies[item["movie_id"]], tag_labels)
            ledger.write_decision(
                build_decision_record(
                    rehydrate(by_id[item["movie_id"]], film),
                    verdict=("spared" if item["verdict"] == "keep" else "queued for review"),
                    reason=item["reason"],
                    run_id=sources.run_id,
                    now=now,
                    monitored=bool(film.tmdb_id and film.tmdb_id in monitored),
                )
            )

        summary = {
            "run_id": sources.run_id,
            "mode": "dry-run" if ledger.dry_run else "act",
            "keep_tags_added": tagged,
            "deferred": [a.film.movie_id for a in deferred],
            "rejected_recommendations": rejected,
            **result.as_dict(),
        }
        Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log(
            f"deleted={len(result.deleted)} skipped={len(result.skipped)} "
            f"failed={len(result.failed)} uncertain={len(result.uncertain)}"
        )
        return 0
    finally:
        ledger.release_lock()


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Run the fixture test suite."""
    suite = unittest.TestLoader().discover(
        str(Path(__file__).parent / "tests"),
        top_level_dir=str(Path(__file__).parent.parent),
    )
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(prog="curator", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="assess the library; writes no changes")
    plan.add_argument("--out", default="./cleanup-plan")
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("execute", help="apply recommendations against a plan")
    run.add_argument("--plan", required=True)
    run.add_argument("--recommendations", required=True)
    run.add_argument("--out", default="./cleanup-result.json")
    run.set_defaults(func=cmd_execute)

    test = sub.add_parser("selftest", help="run the fixture tests")
    test.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
