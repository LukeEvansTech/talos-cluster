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


RESTORE_FIELDS = (
    "tmdbId",
    "imdbId",
    "titleSlug",
    "title",
    "year",
    "path",
    "rootFolderPath",
    "folderName",
    "qualityProfileId",
    "minimumAvailability",
    "monitored",
    "tags",
)


def restore_snapshot(live: dict) -> dict[str, Any]:
    """The fields needed to re-create a movie record exactly as it was.

    Captured from live Radarr before the delete, because afterwards there is
    nowhere left to read them from.
    """
    snapshot = {key: live.get(key) for key in RESTORE_FIELDS}
    movie_file = live.get("movieFile") or {}
    snapshot["file_relative_path"] = movie_file.get("relativePath")
    snapshot["file_size"] = movie_file.get("size")
    return snapshot


def revalidate(
    movie_id: int,
    radarr: Radarr,
    tag_labels: dict[int, str],
    monitored_collection_tmdb_ids: set[int],
    now_playing_ids: set[tuple[str, str]],
) -> tuple[bool, str, dict | None]:
    """Re-check every protection against live state, and return the live record."""
    live = radarr.movie(movie_id)
    if live is None:
        return False, "film is no longer in Radarr", None

    labels = {tag_labels.get(t, str(t)) for t in live.get("tags", [])}
    vetoes = sorted(labels & VETO_TAGS)
    if vetoes:
        return False, f"protection tag added since planning: {', '.join(vetoes)}", live

    tmdb_id = live.get("tmdbId")
    if tmdb_id and int(tmdb_id) in monitored_collection_tmdb_ids:
        return False, "film is in a monitored collection", live

    if live.get("status") != "released":
        return False, f"status is now {live.get('status')}", live

    if not live.get("hasFile"):
        return False, "film no longer has a file", live

    identity = {("tmdb", str(tmdb_id))} if tmdb_id else set()
    if live.get("imdbId"):
        identity.add(("imdb", str(live["imdbId"])))
    if identity & now_playing_ids:
        return False, "somebody is watching it right now", live

    return True, "all protections re-checked against live state", live


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

    # Recoverability is a precondition of deleting, and the plan's copy of it can
    # be minutes or days old. Read it from live Radarr instead.
    bin_ok, bin_detail = recycle_bin_ready(radarr.media_management())
    if not bin_ok:
        result.skipped.extend(
            {
                "movie_id": a.film.movie_id,
                "title": a.film.title,
                "why": f"recoverability precondition failed: {bin_detail}",
            }
            for a in approved
        )
        return result

    try:
        now_playing = tautulli.now_playing_ids() if tautulli else set()
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
            # An unavailable safety check is an unresolved safety check.
            result.skipped.append({**record, "why": "could not confirm nobody is watching it"})
            continue

        try:
            ok, why, live = revalidate(
                film.movie_id,
                radarr,
                tag_labels,
                monitored_collection_tmdb_ids,
                now_playing,
            )
        except SourceError as exc:
            result.skipped.append({**record, "why": f"could not re-read live state: {exc}"})
            continue
        if not ok:
            result.skipped.append({**record, "why": why})
            continue

        ledger.record_intent({**record, "restore": restore_snapshot(live or {})}, assessment.to_json())

        if dry_run:
            result.deleted.append({**record, "simulated": True, "revalidation": why})
            continue

        _delete_one(radarr, ledger, film.movie_id, record, result)

    return result


def _delete_one(radarr: Radarr, ledger, movie_id: int, record: dict, result: ExecutionResult) -> None:
    """Issue one deletion and record an honest outcome for it."""
    try:
        status, _ = radarr.delete_movie(movie_id, add_exclusion=True)
    except SourceError as exc:
        # A transport failure is genuinely ambiguous: the DELETE may have landed.
        # Reconcile by looking, never by retrying.
        try:
            live = radarr.movie(movie_id)
        except SourceError as read_exc:
            ledger.record_outcome(movie_id, "uncertain", f"{exc}; re-read also failed: {read_exc}")
            result.uncertain.append({**record, "why": f"{exc}; could not verify"})
            return
        if live is None:
            ledger.record_outcome(movie_id, "deleted", f"confirmed after transport error: {exc}")
            result.deleted.append({**record, "note": "confirmed by re-read after a transport error"})
        else:
            ledger.record_outcome(movie_id, "uncertain", str(exc))
            result.uncertain.append({**record, "why": str(exc)})
        return

    if not 200 <= status < 300:
        ledger.record_outcome(movie_id, "failed", f"HTTP {status}")
        result.failed.append({**record, "http": status})
        return

    try:
        live = radarr.movie(movie_id)
    except SourceError as exc:
        ledger.record_outcome(movie_id, "uncertain", f"HTTP {status} but verification failed: {exc}")
        result.uncertain.append({**record, "why": f"HTTP {status}, verification failed"})
        return
    if live is None:
        ledger.record_outcome(movie_id, "deleted", f"HTTP {status}, verified gone")
        result.deleted.append({**record, "http": status})
    else:
        ledger.record_outcome(movie_id, "uncertain", f"HTTP {status} but the record is still present")
        result.uncertain.append({**record, "why": f"HTTP {status} but still present"})


def reconcile(unreconciled: list[dict], radarr: Radarr, ledger) -> list[dict]:
    """Resolve intents left dangling by an interrupted run.

    Against current state, never a blind retry: a deletion that timed out may
    well have landed. The outcome is written under the *intent's* run, or
    ``unreconciled_intents`` would never stop returning it.
    """
    resolved = []
    for record in unreconciled:
        movie_id = (record.get("movie") or {}).get("movie_id")
        run_id = record.get("run_id")
        if not isinstance(movie_id, int) or not run_id:
            continue
        try:
            live = radarr.movie(movie_id)
        except SourceError as exc:
            resolved.append({"movie_id": movie_id, "status": "unresolved", "detail": str(exc)})
            continue
        status = "deleted" if live is None else "not-applied"
        ledger.record_outcome(
            movie_id,
            status,
            f"reconciled at {datetime.now(timezone.utc).isoformat()}",
            run_id=run_id,
        )
        resolved.append({"movie_id": movie_id, "status": status, "from_run": run_id})
    return resolved
