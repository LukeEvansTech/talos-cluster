"""Validation and execution of the deletions Claude recommended.

Nothing here trusts the plan. A plan is minutes old by the time it is acted on,
and in those minutes a person can add a protection tag, a show can be
requested, or somebody can start watching it. Every safeguard is re-evaluated
against live state immediately before each deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .clients import Sonarr, SourceError, Tautulli, find_request, guid_ids
from .evidence import COMPLETION_PERCENT, IMPORT_EVENT
from .model import TAG_HUMAN_DISMISSED, VETO_TAGS, Assessment, Show, utc

# Below this a reason cannot name anything specific to the show.
MIN_REASON_LENGTH = 12


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


def validate_recommendations(recommendations: list[dict], candidates: dict[int, Any]) -> tuple[list[dict], list[dict]]:
    """Keep only well-formed recommendations that name an actual candidate.

    A recommendation for a series the engine never offered is not a judgement
    call that went the wrong way -- it is a sign the two halves disagree about
    what was being judged. Both ``delete`` and ``keep`` need a stated reason: a
    keep becomes a permanent ``keep`` tag here, so it is as consequential as a
    delete, only in the other direction.
    """
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen: set[int] = set()
    for item in recommendations:
        if not isinstance(item, dict):
            rejected.append({"item": item, "why": "recommendation is not an object"})
            continue
        series_id = item.get("series_id")
        verdict = str(item.get("verdict", "")).lower()
        reason = str(item.get("reason", "")).strip()
        if not isinstance(series_id, int) or isinstance(series_id, bool):
            rejected.append({"item": item, "why": "series_id missing or not an integer"})
            continue
        if series_id not in candidates:
            rejected.append({"series_id": series_id, "why": "not in this run's candidate set"})
            continue
        if verdict not in ("delete", "keep", "review"):
            rejected.append({"series_id": series_id, "why": f"unknown verdict {verdict!r}"})
            continue
        if verdict in ("delete", "keep") and len(reason) < MIN_REASON_LENGTH:
            rejected.append({"series_id": series_id, "why": f"{verdict} without a stated reason"})
            continue
        if series_id in seen:
            # A series judged twice has been judged two ways. Taking both would
            # delete it and then record a decision to spare it.
            rejected.append({"series_id": series_id, "why": "judged more than once"})
            continue
        seen.add(series_id)
        accepted.append({"series_id": series_id, "verdict": verdict, "reason": reason})
    return accepted, rejected


def recycle_bin_ready(media_management: dict) -> tuple[bool, str]:
    """Whether a deletion here is actually recoverable.

    ``deleteFiles=true`` only moves files aside when a recycle bin is
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
    "tvdbId",
    "tmdbId",
    "imdbId",
    "titleSlug",
    "title",
    "year",
    "path",
    "rootFolderPath",
    "qualityProfileId",
    "seriesType",
    "seasonFolder",
    "monitored",
    "monitorNewItems",
    "tags",
)


def restore_snapshot(live: dict) -> dict[str, Any]:
    """The fields needed to re-create a series record exactly as it was.

    Captured from live Sonarr before the delete, because afterwards there is
    nowhere left to read them from. Per-season monitoring is kept too: re-adding
    a series monitors every season by default.
    """
    snapshot = {key: live.get(key) for key in RESTORE_FIELDS}
    snapshot["seasons"] = [
        {"seasonNumber": s.get("seasonNumber"), "monitored": s.get("monitored")} for s in live.get("seasons") or []
    ]
    stats = live.get("statistics") or {}
    snapshot["episode_file_count"] = stats.get("episodeFileCount")
    snapshot["size_on_disk"] = stats.get("sizeOnDisk")
    return snapshot


def show_identity(tvdb_id, tmdb_id, imdb_id) -> set[tuple[str, str]]:
    """``(source, id)`` pairs for a series, as Plex guids spell them."""
    identity = set()
    if tvdb_id:
        identity.add(("tvdb", str(tvdb_id)))
    if tmdb_id:
        identity.add(("tmdb", str(tmdb_id)))
    if imdb_id:
        identity.add(("imdb", str(imdb_id)))
    return identity


def revalidate(
    series_id: int,
    sonarr: Sonarr,
    tag_labels: dict[int, str],
    now_playing_ids: set[tuple[str, str]],
) -> tuple[bool, str, dict | None]:
    """Re-check every protection against live state, and return the live record."""
    live = sonarr.series(series_id)
    if live is None:
        return False, "series is no longer in Sonarr", None

    labels = {tag_labels.get(t, str(t)) for t in live.get("tags", [])}
    vetoes = sorted(labels & (VETO_TAGS | {TAG_HUMAN_DISMISSED}))
    if vetoes:
        return False, f"protection tag added since planning: {', '.join(vetoes)}", live

    if live.get("status") not in ("continuing", "ended"):
        return False, f"status is now {live.get('status')}", live

    if not int((live.get("statistics") or {}).get("episodeFileCount") or 0):
        return False, "series no longer has any episode on disk", live

    if show_identity(live.get("tvdbId"), live.get("tmdbId"), live.get("imdbId")) & now_playing_ids:
        return False, "somebody is watching it right now", live

    return True, "all protections re-checked against live state", live


def new_content_block(
    series_id: int,
    sonarr: Sonarr,
    live: dict,
    since: datetime | None,
    planned_files: int | None,
) -> str | None:
    """Whether the series gained episodes after the plan was made.

    A new episode restarts a continuing show's window, and nothing else in the
    live re-checks sees one arrive. Any import after the plan counts, upgrades
    included: refusing a series for a week costs nothing, deleting a new season
    the day it lands does.
    """
    if since is None:
        return "cannot tell whether episodes arrived since the plan"
    files = int((live.get("statistics") or {}).get("episodeFileCount") or 0)
    if planned_files is not None and files > planned_files:
        return f"episodes on disk grew from {planned_files} to {files} since the plan"
    for event in sonarr.series_import_history(series_id):
        if event.get("eventType") != IMPORT_EVENT:
            continue
        when = utc(event.get("date"))
        if when is None or when > since:
            return "an episode was imported since the plan was made"
    return None


def late_evidence(tautulli, seerr, crosswalk: dict[int, dict] | None, since: datetime | None):
    """Re-read the two things that change outside Sonarr, at execution time.

    Only evidence dated **after** ``since`` -- the moment the plan was made --
    counts. A series one person has been watching reaches the candidate list by
    design, so treating those pre-existing plays as new would silently overturn
    that rule. An episode finished since the plan does block.

    Returns ``(requests, watched_ids, known)``. ``known`` is False when either
    source could not be read -- an unavailable safety check is an unresolved
    one, so the caller skips rather than proceeding.
    """
    requests: dict[tuple[str, int], dict] = {}
    watched: set[tuple[str, str]] = set()
    if since is None:
        return {}, set(), False

    if seerr is not None:
        try:
            for key, request in seerr.tv_requests().items():
                created = utc(request.get("created_at"))
                if created and created > since:
                    requests[key] = request
        except SourceError:
            return {}, set(), False

    if tautulli is None:
        return requests, watched, False
    try:
        rows, meta = tautulli.episode_history()
    except SourceError:
        return {}, set(), False
    if not meta.get("complete"):
        return {}, set(), False

    cutoff = since.timestamp()
    by_key: dict[int, dict] = dict(crosswalk or {})
    for row in rows:
        if int(row.get("percent_complete") or 0) < COMPLETION_PERCENT:
            continue
        stopped = row.get("stopped") or row.get("date")
        if not str(stopped).isdigit() or int(stopped) <= cutoff:
            continue
        key = row.get("grandparent_rating_key")
        if key in (None, "") or not str(key).isdigit():
            # A finished episode we cannot tie to a show: refuse rather than guess.
            return requests, watched, False

        entry = by_key.get(int(key))
        if entry is None:
            # The crosswalk only has shows seen by the plan; one played for the first
            # time since is missing from it, exactly the play this gate exists to catch.
            entry = _resolve_key(tautulli, int(key))
            by_key[int(key)] = entry
        ids = show_identity(entry.get("tvdb"), entry.get("tmdb"), entry.get("imdb"))
        if not ids:
            return requests, watched, False
        watched |= ids
    return requests, watched, True


def _resolve_key(tautulli, rating_key: int) -> dict:
    """Look up one Plex show's external ids, for a key seen since the plan."""
    try:
        meta = tautulli.metadata(rating_key)
    except SourceError:
        return {"unresolved": True}
    if not meta:
        return {"unresolved": True}
    ids = dict(guid_ids(meta.get("guids")))
    return ids or {"unresolved": True}


def late_evidence_block(
    show: Show, requests: dict[tuple[str, int], dict], watched_ids: set[tuple[str, str]]
) -> str | None:
    """Whether something happened between planning and now that protects a series.

    Live re-validation covers tags, status, file presence and current playback
    -- all state Sonarr holds. It does not cover the two things that change
    outside Sonarr: somebody requesting a show, and somebody finishing an episode.
    """
    request = find_request(requests, show.tvdb_id, show.tmdb_id)
    if request is not None:
        who = (request or {}).get("requested_by") or "someone"
        return f"requested by {who} since the plan was made"
    if show_identity(show.tvdb_id, show.tmdb_id, show.imdb_id) & watched_ids:
        return "an episode was finished since the plan was made"
    return None


def volatile_block(
    show: Show, tautulli, seerr, crosswalk: dict[int, dict] | None, since: datetime | None
) -> str | None:
    """Re-read, for one series, the evidence that moves while the batch is running.

    ``revalidate`` re-reads Sonarr per series, but the checks outside Sonarr --
    who is watching now, and what has been requested or finished since the plan
    -- were read once, before the loop. Starting an episode takes seconds, so
    this runs immediately before the destructive call, and an answer it cannot
    get is a refusal.
    """
    if tautulli is None:
        return "could not re-confirm nobody is watching it"
    try:
        playing = tautulli.now_playing_ids()
    except SourceError as exc:
        return f"could not re-confirm nobody is watching it: {exc}"

    if show_identity(show.tvdb_id, show.tmdb_id, show.imdb_id) & playing:
        return "somebody started watching it during this run"

    requests, watched_ids, known = late_evidence(tautulli, seerr, crosswalk, since)
    if not known:
        return "could not re-read plays and requests during this run"
    return late_evidence_block(show, requests, watched_ids)


def execute(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    approved: list[Assessment],
    sonarr: Sonarr,
    tautulli: Tautulli | None,
    ledger,
    tag_labels: dict[int, str],
    dry_run: bool = True,
    seerr=None,
    crosswalk: dict[int, dict] | None = None,
    planned_at: datetime | None = None,
    planned_files: dict[int, int] | None = None,
) -> ExecutionResult:
    """Delete the approved series, re-validating each one first.

    ``planned_files`` is each series' episode-file count as the plan saw it.
    """
    result = ExecutionResult()

    # Recoverability is a precondition of deleting, and the plan's copy of it can
    # be minutes or days old. Read it from live Sonarr instead.
    bin_ok, bin_detail = recycle_bin_ready(sonarr.media_management())
    if not bin_ok:
        result.skipped.extend(
            {
                "series_id": a.show.series_id,
                "title": a.show.title,
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

    requests, watched_ids, late_known = late_evidence(tautulli, seerr, crosswalk, planned_at)
    if not late_known:
        result.skipped.extend(
            {
                "series_id": a.show.series_id,
                "title": a.show.title,
                "why": "could not re-read plays and requests made since the plan",
            }
            for a in approved
        )
        return result

    for assessment in approved:
        show = assessment.show
        record = {
            "series_id": show.series_id,
            "tvdb_id": show.tvdb_id,
            "title": show.title,
            "year": show.year,
            "size_gb": show.size_gb,
        }

        if not activity_known:
            # An unavailable safety check is an unresolved safety check.
            result.skipped.append({**record, "why": "could not confirm nobody is watching it"})
            continue

        late = late_evidence_block(show, requests, watched_ids)
        if late:
            result.skipped.append({**record, "why": late})
            continue

        try:
            ok, why, live = revalidate(show.series_id, sonarr, tag_labels, now_playing)
            grown = (
                new_content_block(
                    show.series_id, sonarr, live or {}, planned_at, (planned_files or {}).get(show.series_id)
                )
                if ok
                else None
            )
        except SourceError as exc:
            result.skipped.append({**record, "why": f"could not re-read live state: {exc}"})
            continue
        if not ok:
            result.skipped.append({**record, "why": why})
            continue
        if grown:
            result.skipped.append({**record, "why": grown})
            continue

        # Last, the check closest in time to the delete; run in dry-run too, or a report
        # would describe a batch nobody is going to run.
        moved = volatile_block(show, tautulli, seerr, crosswalk, planned_at)
        if moved:
            result.skipped.append({**record, "why": moved})
            continue

        ledger.record_intent({**record, "restore": restore_snapshot(live or {})}, assessment.to_json())

        if dry_run:
            result.deleted.append({**record, "simulated": True, "revalidation": why})
            continue

        _delete_one(sonarr, ledger, show.series_id, record, result)

    return result


def _delete_one(sonarr: Sonarr, ledger, series_id: int, record: dict, result: ExecutionResult) -> None:
    """Issue one deletion and record an honest outcome for it."""
    try:
        status, _ = sonarr.delete_series(series_id, add_exclusion=True)
    except SourceError as exc:
        # A transport failure is genuinely ambiguous: the DELETE may have landed.
        # Reconcile by looking, never by retrying.
        try:
            live = sonarr.series(series_id)
        except SourceError as read_exc:
            ledger.record_outcome(series_id, "uncertain", f"{exc}; re-read also failed: {read_exc}")
            result.uncertain.append({**record, "why": f"{exc}; could not verify"})
            return
        if live is None:
            ledger.record_outcome(series_id, "deleted", f"confirmed after transport error: {exc}")
            result.deleted.append({**record, "note": "confirmed by re-read after a transport error"})
        else:
            ledger.record_outcome(series_id, "uncertain", str(exc))
            result.uncertain.append({**record, "why": str(exc)})
        return

    if not 200 <= status < 300:
        ledger.record_outcome(series_id, "failed", f"HTTP {status}")
        result.failed.append({**record, "http": status})
        return

    try:
        live = sonarr.series(series_id)
    except SourceError as exc:
        ledger.record_outcome(series_id, "uncertain", f"HTTP {status} but verification failed: {exc}")
        result.uncertain.append({**record, "why": f"HTTP {status}, verification failed"})
        return
    if live is None:
        ledger.record_outcome(series_id, "deleted", f"HTTP {status}, verified gone")
        result.deleted.append({**record, "http": status})
    else:
        ledger.record_outcome(series_id, "uncertain", f"HTTP {status} but the record is still present")
        result.uncertain.append({**record, "why": f"HTTP {status} but still present"})


def reconcile(unreconciled: list[dict], sonarr: Sonarr, ledger) -> list[dict]:
    """Resolve intents left dangling by an interrupted run.

    Against current state, never a blind retry: a deletion that timed out may
    well have landed. The outcome is written under the *intent's* run, or
    ``unreconciled_intents`` would never stop returning it.
    """
    resolved = []
    for record in unreconciled:
        series_id = (record.get("series") or {}).get("series_id")
        run_id = record.get("run_id")
        if not isinstance(series_id, int) or not run_id:
            continue
        try:
            live = sonarr.series(series_id)
        except SourceError as exc:
            resolved.append({"series_id": series_id, "status": "unresolved", "detail": str(exc)})
            continue
        status = "deleted" if live is None else "not-applied"
        ledger.record_outcome(
            series_id,
            status,
            f"reconciled at {datetime.now(timezone.utc).isoformat()}",
            run_id=run_id,
        )
        resolved.append({"series_id": series_id, "status": status, "from_run": run_id})
    return resolved
