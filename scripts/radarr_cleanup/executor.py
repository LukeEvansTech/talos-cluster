"""Validation and execution of the deletions Claude recommended.

Nothing here trusts the plan. A plan is minutes old by the time it is acted on,
and in those minutes a person can add a protection tag, a collection can be set
to monitored, or somebody can start watching the film. Every safeguard is
re-evaluated against live state immediately before each deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .clients import Radarr, SourceError, Tautulli
from .model import VETO_TAGS, Assessment


@dataclass
class ExecutionResult:
    """What one run actually did."""

    deleted: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)
    uncertain: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the report."""
        return {
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failed": self.failed,
            "uncertain": self.uncertain,
        }


def validate_recommendations(
    recommendations: list[dict], candidates: dict[int, Assessment]
) -> tuple[list[dict], list[dict]]:
    """Keep only well-formed recommendations that name an actual candidate.

    A recommendation for a film the engine never offered is not a judgement call
    that went the wrong way -- it is a sign the two halves disagree about what
    was being judged, and acting on it would bypass every check that produced the
    candidate list.
    """
    accepted: list[dict] = []
    rejected: list[dict] = []
    for item in recommendations:
        movie_id = item.get("movie_id")
        verdict = str(item.get("verdict", "")).lower()
        reason = str(item.get("reason", "")).strip()
        if not isinstance(movie_id, int):
            rejected.append({"item": item, "why": "movie_id missing or not an integer"})
            continue
        if movie_id not in candidates:
            rejected.append({"movie_id": movie_id, "why": "not in this run's candidate set"})
            continue
        if verdict not in ("delete", "keep", "review"):
            rejected.append({"movie_id": movie_id, "why": f"unknown verdict {verdict!r}"})
            continue
        if verdict == "delete" and len(reason) < 12:
            rejected.append({"movie_id": movie_id, "why": "delete without a stated reason"})
            continue
        accepted.append({"movie_id": movie_id, "verdict": verdict, "reason": reason})
    return accepted, rejected


def recycle_bin_ready(media_management: dict) -> tuple[bool, str]:
    """Whether a deletion here is actually recoverable.

    ``deleteFiles=true`` only moves a file aside when a recycle bin is
    configured. With the path empty the same call is an unrecoverable delete, and
    nothing in the response distinguishes the two.
    """
    path = (media_management or {}).get("recycleBin") or ""
    days = (media_management or {}).get("recycleBinCleanupDays")
    if not str(path).strip():
        return False, "recycle bin path is empty: deletion would be unrecoverable"
    if not isinstance(days, int) or days <= 0:
        return False, f"recycle bin at {path} has no positive retention ({days!r})"
    return True, f"recycle bin {path}, retention {days} days"


def revalidate(
    movie_id: int,
    radarr: Radarr,
    tag_labels: dict[int, str],
    monitored_collection_tmdb_ids: set[int],
    now_playing: set[int],
    plex_rating_keys: set[int],
) -> tuple[bool, str]:
    """Re-check every protection against live state, immediately before deleting."""
    live = radarr.movie(movie_id)
    if live is None:
        return False, "film is no longer in Radarr"

    labels = {tag_labels.get(t, str(t)) for t in live.get("tags", [])}
    vetoes = sorted(labels & VETO_TAGS)
    if vetoes:
        return False, f"protection tag added since planning: {', '.join(vetoes)}"

    tmdb_id = live.get("tmdbId")
    if tmdb_id and int(tmdb_id) in monitored_collection_tmdb_ids:
        return False, "film is in a monitored collection"

    if live.get("status") != "released":
        return False, f"status is now {live.get('status')}"

    if not live.get("hasFile"):
        return False, "film no longer has a file"

    if plex_rating_keys & now_playing:
        return False, "somebody is watching it right now"

    return True, "all protections re-checked against live state"


def execute(
    approved: list[Assessment],
    radarr: Radarr,
    tautulli: Tautulli | None,
    ledger,
    tag_labels: dict[int, str],
    monitored_collection_tmdb_ids: set[int],
    dry_run: bool = True,
) -> ExecutionResult:
    """Delete the approved films, re-validating each one first."""
    result = ExecutionResult()

    try:
        now_playing = tautulli.now_playing_rating_keys() if tautulli else set()
        activity_known = tautulli is not None
    except SourceError as exc:
        now_playing, activity_known = set(), False
        result.skipped.append({"why": f"activity check unavailable: {exc}"})

    for assessment in approved:
        film = assessment.film
        record = {
            "movie_id": film.movie_id,
            "tmdb_id": film.tmdb_id,
            "title": film.title,
            "year": film.year,
            "size_gb": film.size_gb,
        }

        if not activity_known:
            # An unavailable safety check is an unresolved safety check, not a
            # passed one.
            result.skipped.append({**record, "why": "could not confirm nobody is watching it"})
            continue

        ok, why = revalidate(
            film.movie_id,
            radarr,
            tag_labels,
            monitored_collection_tmdb_ids,
            now_playing,
            set(assessment.viewing.rating_keys),
        )
        if not ok:
            result.skipped.append({**record, "why": why})
            continue

        ledger.record_intent(record, assessment.to_json())

        if dry_run:
            result.deleted.append({**record, "simulated": True, "revalidation": why})
            continue

        try:
            status, _ = radarr.delete_movie(film.movie_id, add_exclusion=True)
        except SourceError as exc:
            # A transport failure is genuinely ambiguous: the DELETE may have
            # been applied. Reconcile rather than retry, or a "failed" delete is
            # repeated against a film that is already gone.
            live = radarr.movie(film.movie_id)
            if live is None:
                ledger.record_outcome(film.movie_id, "deleted", f"confirmed after transport error: {exc}")
                result.deleted.append({**record, "note": "confirmed by re-read after a transport error"})
            else:
                ledger.record_outcome(film.movie_id, "uncertain", str(exc))
                result.uncertain.append({**record, "why": str(exc)})
            continue

        if 200 <= status < 300:
            live = radarr.movie(film.movie_id)
            if live is None:
                ledger.record_outcome(film.movie_id, "deleted", f"HTTP {status}, verified gone")
                result.deleted.append({**record, "http": status})
            else:
                ledger.record_outcome(film.movie_id, "uncertain", f"HTTP {status} but the record is still present")
                result.uncertain.append({**record, "why": f"HTTP {status} but still present"})
        else:
            ledger.record_outcome(film.movie_id, "failed", f"HTTP {status}")
            result.failed.append({**record, "http": status})

    return result


def reconcile(unreconciled: list[dict], radarr: Radarr, ledger) -> list[dict]:
    """Resolve intents left dangling by an interrupted run.

    Reconciliation is against current state, never a blind retry: a deletion that
    timed out may well have landed.
    """
    resolved = []
    for record in unreconciled:
        movie_id = (record.get("movie") or {}).get("movie_id")
        if not isinstance(movie_id, int):
            continue
        live = radarr.movie(movie_id)
        status = "deleted" if live is None else "not-applied"
        ledger.record_outcome(
            movie_id,
            status,
            f"reconciled from run {record.get('run_id')} at {datetime.now(timezone.utc).isoformat()}",
        )
        resolved.append({"movie_id": movie_id, "status": status, "from_run": record.get("run_id")})
    return resolved
