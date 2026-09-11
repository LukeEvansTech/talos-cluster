"""Command-line entry point: ``plan``, ``execute``, ``reconcile`` and ``selftest``.

``plan`` is read-only and always safe to run. ``execute`` is the only command that
can change anything, and only when ``--mode act`` is passed *and* the environment
agrees.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .clients import Radarr, RequestSystem, SourceError, Tautulli
from .evidence import build_crosswalk, resolve_availability, resolve_provenance, resolve_viewing
from .executor import execute, recycle_bin_ready, reconcile, validate_recommendations
from .ledger import Ledger
from .model import (
    TAG_KEEP,
    TAG_KEEP_REVIEW,
    Film,
    Outcome,
    utc,
)
from .planner import (
    MAX_DELETIONS_PER_RUN,
    PlanContext,
    keep_tag_targets,
    build_decision_record,
    check_anomalies,
    in_monitored_collection,
    select_batch,
    snapshot_valid,
    summarise,
)
from .s3 import S3Client

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


def build_sources() -> tuple[Radarr, Tautulli, RequestSystem | None, Ledger, str]:
    """Construct every client from the environment."""
    run_id = os.environ.get("CLEANUP_RUN_ID") or f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    radarr = Radarr(env("RADARR_URL"), env("RADARR_API_KEY"))
    tautulli = Tautulli(env("TAUTULLI_URL"), env("TAUTULLI_API_KEY"))
    seerr_url = env("SEERR_URL", required=False)
    seerr = RequestSystem(seerr_url, env("SEERR_API_KEY", required=False)) if seerr_url else None
    ledger = Ledger(
        S3Client(
            env("LEDGER_ENDPOINT"),
            env("LEDGER_ACCESS_KEY_ID"),
            env("LEDGER_SECRET_ACCESS_KEY"),
            env("LEDGER_BUCKET"),
        ),
        run_id=run_id,
        dry_run=os.environ.get("CLEANUP_MODE", "dry-run").lower() != "act",
    )
    return radarr, tautulli, seerr, ledger, run_id


def cmd_plan(args: argparse.Namespace) -> int:
    """Assess the whole library and emit the candidate set for judgement."""
    radarr, tautulli, seerr, ledger, run_id = build_sources()
    now = datetime.now(timezone.utc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    log(f"run {run_id} (mode={'DRY RUN' if ledger.dry_run else 'ACT'})")
    status = radarr.status()
    log(f"Radarr {status.get('version')} reachable")
    media_management = radarr.media_management()
    bin_ok, bin_detail = recycle_bin_ready(media_management)
    log(f"recycle bin: {bin_detail}")

    history_ok = True
    history_note = ""
    try:
        tautulli.preflight()
        rows, history_meta = tautulli.movie_history()
        log(f"play history: {history_meta['retrieved']} rows (declared {history_meta['declared']})")
        if not history_meta["complete"]:
            history_ok = False
            history_note = "retrieved fewer rows than the service declared"
    except SourceError as exc:
        rows, history_meta = [], {"declared": None, "retrieved": 0, "complete": False}
        history_ok = False
        history_note = str(exc)
        log(f"play history UNAVAILABLE: {exc}")

    tag_labels = {t["id"]: t["label"] for t in radarr.tags()}
    movies = radarr.movies()
    collections = radarr.collections()
    exclusions = radarr.exclusions()
    import_events = radarr.import_events()
    horizon = utc(radarr.history_horizon())
    log(f"library {len(movies)} films, {len(collections)} collections, {len(exclusions)} exclusions")
    log(f"Radarr history reaches back to {horizon.date() if horizon else 'unknown'}")

    requests_by_tmdb: dict[int, dict] = {}
    if seerr:
        try:
            requests_by_tmdb = seerr.movie_requests()
            log(f"request system: {len(requests_by_tmdb)} movie requests")
        except SourceError as exc:
            log(f"request system unavailable: {exc} -- every origin will read as unknown")

    monitored_tmdb = {
        entry.get("tmdbId")
        for collection in collections
        if collection.get("monitored")
        for entry in (collection.get("movies") or [])
        if entry.get("tmdbId")
    }
    log(f"{sum(1 for c in collections if c.get('monitored'))} monitored collections protect {len(monitored_tmdb)} tmdb ids")

    # --- Plex identity crosswalk -----------------------------------------
    crosswalk = ledger.read_crosswalk()
    rows_by_key: dict[int, list[dict]] = {}
    for row in rows:
        key = row.get("rating_key")
        if key in (None, ""):
            continue
        rows_by_key.setdefault(int(key), []).append(row)
    missing = [k for k in rows_by_key if k not in crosswalk]
    log(f"resolving {len(missing)} new Plex rating keys ({len(crosswalk)} cached)")
    fresh: dict[int, dict] = {}
    retired: dict[int, dict] = {}
    for key in missing:
        meta = tautulli.metadata(key)
        if meta:
            fresh[key] = meta
        else:
            # Plex reissues rating keys, and get_metadata 404s on the old one.
            # Remember the failure with the title the history rows carry, so the
            # next run does not re-ask and the plays stay attributable by title.
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
    unattributed_rows = [row for key in unattributed_keys for row in rows_by_key.get(key, [])]
    if unattributed_rows:
        log(
            f"{len(unattributed_rows)} play(s) across {len(unattributed_keys)} retired Plex items "
            "cannot be attributed by id; films sharing their titles go to review"
        )

    coverage_start = None
    if rows:
        stamps = [int(r["date"]) for r in rows if str(r.get("date", "")).isdigit()]
        if stamps:
            coverage_start = datetime.fromtimestamp(min(stamps), tz=timezone.utc)
    log(f"play history covers from {coverage_start.date() if coverage_start else 'unknown'}")

    ctx = PlanContext(
        now=now,
        scope_start=utc(os.environ.get("CLEANUP_SCOPE_START") or DEFAULT_SCOPE_START) or now,
        monitored_collection_tmdb_ids=frozenset(monitored_tmdb),
        coverage_start=coverage_start,
        history_ok=history_ok,
        decisions=ledger.read_decisions(),
        collections_loaded=bool(collections),
    )

    first_seen = ledger.read_first_seen()
    assessments = []
    new_first_seen: dict[int, datetime] = {}
    from .planner import assess  # local import keeps the module import graph flat

    for record in movies:
        film = to_film(record, tag_labels)
        provenance = resolve_provenance(film, requests_by_tmdb)
        availability = resolve_availability(
            film, import_events.get(film.movie_id, []), horizon, first_seen.get(film.movie_id), now
        )
        if availability.source == "ledger-bootstrap" and availability.first_playable:
            new_first_seen[film.movie_id] = availability.first_playable
        viewing = resolve_viewing(
            film,
            rows_by_key,
            crosswalk,
            coverage_start,
            availability,
            history_ok=history_ok,
            unattributed_rows=unattributed_rows,
        )
        assessments.append(assess(film, provenance, availability, viewing, ctx))

    if new_first_seen:
        ledger.write_first_seen(new_first_seen)

    counts = summarise(assessments)
    log(f"assessment: {counts}")

    snapshot = {
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
    baseline = ledger.read_baseline()
    blocks = check_anomalies(snapshot, baseline)
    if not bin_ok:
        blocks.append(bin_detail)
    if not history_ok:
        blocks.append(f"play history not usable: {history_note}")

    problems = snapshot_valid(snapshot)
    if problems:
        log(f"snapshot rejected as a baseline: {problems}")
    elif not blocks or baseline is None:
        ledger.write_baseline(snapshot)
        log("baseline recorded")

    keep_targets = keep_tag_targets([a.film for a in assessments])
    log(f"{len(keep_targets)} films qualify for the keep tag and do not carry it")

    candidates = [a for a in assessments if a.outcome is Outcome.CANDIDATE]
    payload = {
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "mode": "dry-run" if ledger.dry_run else "act",
        "scope_start": ctx.scope_start.isoformat(),
        "counts": counts,
        "snapshot": snapshot,
        "blocks": blocks,
        "recycle_bin": bin_detail,
        "history": history_meta,
        "history_coverage_start": coverage_start.isoformat() if coverage_start else None,
        "unattributed_plays": len(unattributed_rows),
        "radarr_history_horizon": horizon.isoformat() if horizon else None,
        "keep_tag_additions": [
            {"movie_id": f.movie_id, "title": f.title, "imdb_score": f.imdb_score, "imdb_votes": f.imdb_votes}
            for f in keep_targets
        ],
        "candidates": [a.to_json() for a in candidates],
        "review": [a.to_json() for a in assessments if a.outcome is Outcome.REVIEW],
        "missing_file": [
            a.to_json() for a in assessments if a.outcome is Outcome.MISSING_FILE
        ],
        "multi_user_completions": [
            a.to_json() for a in assessments if a.viewing.distinct_completers >= 2
        ],
        "expired_basis_keeps": [
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
        ],
    }
    (out / "plan.json").write_text(json.dumps(payload, indent=2))
    ledger.record_plan({k: v for k, v in payload.items() if k != "review"})
    log(f"wrote {out / 'plan.json'}: {len(candidates)} candidates, {len(blocks)} blocking issues")
    for block in blocks:
        log(f"  BLOCK: {block}")
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    """Apply Claude's recommendations, re-validating every safeguard first."""
    radarr, tautulli, _seerr, ledger, run_id = build_sources()
    plan = json.loads(Path(args.plan).read_text())
    recommendations = json.loads(Path(args.recommendations).read_text())
    if isinstance(recommendations, dict):
        recommendations = recommendations.get("recommendations", [])

    if plan.get("blocks"):
        log("refusing to execute: the plan recorded blocking issues")
        for block in plan["blocks"]:
            log(f"  BLOCK: {block}")
        return 2

    tag_labels = {t["id"]: t["label"] for t in radarr.tags()}
    collections = radarr.collections()
    monitored_tmdb = {
        entry.get("tmdbId")
        for collection in collections
        if collection.get("monitored")
        for entry in (collection.get("movies") or [])
        if entry.get("tmdbId")
    }

    by_id = {}
    for item in plan.get("candidates", []):
        by_id[item["movie_id"]] = item

    accepted, rejected = validate_recommendations(recommendations, by_id)
    for bad in rejected:
        log(f"  rejected recommendation: {bad}")

    dangling = ledger.unreconciled_intents()
    if dangling:
        log(f"reconciling {len(dangling)} interrupted deletions from earlier runs")
        for entry in reconcile(dangling, radarr, ledger):
            log(f"  {entry}")

    ok, acquired = ledger.acquire_lock()
    if not ok:
        log(f"refusing to execute: {acquired}")
        return 3

    try:
        movies = {m["id"]: m for m in radarr.movies()}
        approved = []
        from .model import Assessment, Availability, HistoryStatus, Provenance, Origin, Viewing

        for item in accepted:
            if item["verdict"] != "delete":
                continue
            record = movies.get(item["movie_id"])
            if record is None:
                log(f"  {item['movie_id']} is already gone")
                continue
            planned = by_id[item["movie_id"]]
            film = to_film(record, tag_labels)
            approved.append(
                Assessment(
                    film=film,
                    outcome=Outcome.CANDIDATE,
                    provenance=Provenance(Origin(planned["origin"]), tuple(planned["origin_evidence"])),
                    availability=Availability(None, planned["availability_source"], film.has_file),
                    viewing=Viewing(
                        status=HistoryStatus(planned["history_status"]),
                        identity_via=planned["identity_via"],
                        rating_keys=tuple(planned.get("rating_keys", []) or []),
                    ),
                    reasons=[item["reason"]],
                )
            )

        already = ledger.deletions_in_window()
        selected, deferred = select_batch(approved, MAX_DELETIONS_PER_RUN, already)
        log(f"{len(approved)} approved, {already} already deleted this window, acting on {len(selected)}, deferring {len(deferred)}")

        result = execute(
            selected, radarr, tautulli, ledger, tag_labels, monitored_tmdb, dry_run=ledger.dry_run
        )

        now = datetime.now(timezone.utc)
        for item in accepted:
            if item["verdict"] in ("keep", "review"):
                planned = by_id[item["movie_id"]]
                record = movies.get(item["movie_id"])
                if record is None:
                    continue
                film = to_film(record, tag_labels)
                ledger.write_decision(
                    build_decision_record(
                        Assessment(
                            film=film,
                            outcome=Outcome.REVIEW,
                            provenance=Provenance(Origin(planned["origin"])),
                            availability=Availability(None, planned["availability_source"], film.has_file),
                            viewing=Viewing(status=HistoryStatus(planned["history_status"])),
                        ),
                        verdict="spared" if item["verdict"] == "keep" else "queued for review",
                        reason=item["reason"],
                        run_id=run_id,
                        now=now,
                        monitored=bool(film.tmdb_id and film.tmdb_id in monitored_tmdb),
                    )
                )

        summary = {
            "run_id": run_id,
            "mode": "dry-run" if ledger.dry_run else "act",
            "deferred": [a.film.movie_id for a in deferred],
            "rejected_recommendations": rejected,
            **result.as_dict(),
        }
        Path(args.out).write_text(json.dumps(summary, indent=2))
        log(
            f"deleted={len(result.deleted)} skipped={len(result.skipped)} "
            f"failed={len(result.failed)} uncertain={len(result.uncertain)}"
        )
        return 0
    finally:
        ledger.release_lock()


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Run the fixture test suite."""
    import unittest

    loader = unittest.TestLoader()
    suite = loader.discover(str(Path(__file__).parent / "tests"), top_level_dir=str(Path(__file__).parent.parent))
    runner = unittest.TextTestRunner(verbosity=2)
    return 0 if runner.run(suite).wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(prog="radarr_cleanup", description=__doc__)
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
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
