"""Turn raw API payloads into the three pieces of evidence a decision needs.

Pure functions only: every input is already-fetched data, so each rule here is
exercised from a fixture rather than from a live cluster.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .model import (
    Availability,
    Completion,
    Film,
    HistoryStatus,
    Origin,
    Provenance,
    Viewing,
    normalise_title,
    utc,
)

# A play is a "completion" at or above this much of the runtime. Below the
# abandonment mark it is evidence somebody bounced off the film, not evidence of
# value, and between the two it is neither.
COMPLETION_PERCENT = 85
ABANDONED_PERCENT = 20

IMPORT_EVENT = "downloadFolderImported"


# --- 1. Provenance --------------------------------------------------------


def resolve_provenance(film: Film, requests_by_tmdb: dict[int, dict]) -> Provenance:
    """Classify how a film entered the library.

    Only two things are evidence. A request record in the request system proves a
    person asked for it; an import-list provenance tag proves a feed added it.
    Everything else is unknown, and unknown must never be quietly read as "feed"
    to manufacture a deletion candidate -- the date a record was created says
    nothing about who created it.
    """
    request = requests_by_tmdb.get(film.tmdb_id) if film.tmdb_id else None
    if request:
        who = request.get("requested_by") or "unknown user"
        return Provenance(
            origin=Origin.REQUESTED,
            evidence=(f"request system: requested by {who} on {request.get('created_at', '?')}",),
            requested_by=who,
        )

    tags = sorted(film.provenance_tags)
    if tags:
        return Provenance(
            origin=Origin.FEED,
            evidence=(f"provenance tag(s) {', '.join(tags)} applied by the import list",),
        )

    return Provenance(
        origin=Origin.UNKNOWN,
        evidence=("no request record and no import-list provenance tag",),
    )


# --- 2. Availability ------------------------------------------------------


def first_import_from_history(history: list[dict]) -> datetime | None:
    """Earliest successful import for a film, from its Radarr history.

    Deliberately the *earliest*, so a later quality upgrade -- which writes a new
    import event and replaces ``movieFile.dateAdded`` -- cannot restart a viewing
    window that already ran.
    """
    stamps = [
        utc(row.get("date"))
        for row in history
        if row.get("eventType") == IMPORT_EVENT and row.get("date")
    ]
    real = [s for s in stamps if s is not None]
    return min(real) if real else None


def resolve_availability(
    film: Film,
    history: list[dict],
    history_horizon: datetime | None,
    ledger_first_seen: datetime | None,
    now: datetime,
) -> Availability:
    """Decide when a film first became watchable.

    ``added`` is when Radarr learned the film exists, which can be months before a
    file arrives and is sometimes years before -- a request placed pre-release
    waits for the release. The window has to run from the first import, or a film
    nobody could have watched is deleted for not having been watched.
    """
    imported = first_import_from_history(history)
    if imported:
        return Availability(
            first_playable=imported,
            source="radarr-history",
            has_file=film.has_file,
            evidence=(f"first Radarr import event at {imported.isoformat()}",),
        )

    if not film.has_file:
        return Availability(
            first_playable=None,
            source="none",
            has_file=False,
            evidence=("no file on disk and no import event",),
        )

    # A file exists but no import event does. Either the history has aged out or
    # the file was placed on disk outside Radarr. Fall back to a previously
    # persisted observation; failing that, treat *now* as the first observation
    # and persist it, which starts the clock again rather than ending it early.
    if ledger_first_seen:
        return Availability(
            first_playable=ledger_first_seen,
            source="ledger",
            has_file=True,
            evidence=(f"first observed playable by this routine at {ledger_first_seen.isoformat()}",),
        )

    horizon = f" (Radarr history only reaches {history_horizon.isoformat()})" if history_horizon else ""
    return Availability(
        first_playable=now,
        source="ledger-bootstrap",
        has_file=True,
        evidence=(f"no import event in retained history{horizon}; first observed now",),
    )


# --- 3. Viewing evidence --------------------------------------------------


def build_crosswalk(metadata: dict[int, dict]) -> dict[int, dict]:
    """Map Plex rating keys to external IDs from ``get_metadata`` payloads."""
    crosswalk: dict[int, dict] = {}
    for rating_key, meta in metadata.items():
        ids = {"tmdb": None, "imdb": None}
        for guid in meta.get("guids") or []:
            if guid.startswith("tmdb://"):
                suffix = guid.removeprefix("tmdb://")
                ids["tmdb"] = int(suffix) if suffix.isdigit() else None
            elif guid.startswith("imdb://"):
                ids["imdb"] = guid.removeprefix("imdb://")
        crosswalk[int(rating_key)] = {
            "tmdb": ids["tmdb"],
            "imdb": ids["imdb"],
            "title": meta.get("title"),
            "year": int(meta["year"]) if str(meta.get("year", "")).isdigit() else None,
        }
    return crosswalk


def _match_rating_keys(film: Film, crosswalk: dict[int, dict]) -> tuple[list[int], str]:
    """Find the Plex items that are this film, preferring stable identifiers."""
    by_tmdb = [k for k, v in crosswalk.items() if film.tmdb_id and v.get("tmdb") == film.tmdb_id]
    if by_tmdb:
        return by_tmdb, "tmdb"

    by_imdb = [k for k, v in crosswalk.items() if film.imdb_id and v.get("imdb") == film.imdb_id]
    if by_imdb:
        return by_imdb, "imdb"

    # Title and year are a fallback, never a confirmation: remakes share a title
    # and a distributor sometimes ships a different year. A hit here is good
    # enough to *suspect* a match and therefore good enough to refuse a deletion,
    # which is what the caller does with it.
    target = normalise_title(film.title)
    by_title = [
        key
        for key, value in crosswalk.items()
        if not value.get("unresolved")
        and normalise_title(value.get("title")) == target
        and (
            film.year is None
            or value.get("year") is None
            or abs(value["year"] - film.year) <= 1
        )
    ]
    if by_title:
        return by_title, "title-year"
    return [], "unresolved"


def resolve_viewing(
    film: Film,
    rows_by_rating_key: dict[int, list[dict]],
    crosswalk: dict[int, dict],
    coverage_start: datetime | None,
    availability: Availability,
    history_ok: bool = True,
    unattributed_rows: list[dict] | None = None,
) -> Viewing:
    """Assemble play evidence for one film and state how far it can be trusted.

    The join runs from history rows to films, not the other way round. That
    matters because a film nobody has played never appears in the history at all,
    so "I could not find it in the crosswalk" is the *expected* state for exactly
    the films this routine exists to remove -- reading it as a failed lookup puts
    every genuine candidate into review forever.

    A zero is therefore only trustworthy when two things hold: the history was
    retrieved completely, and every rating key in it resolved to an external id.
    Plex reissues rating keys and ``get_metadata`` 404s on retired ones, so the
    plays behind an unresolvable key are real plays belonging to *some* film.
    Those rows are matched back by title and year, and any film they might belong
    to is sent to review rather than being credited with a clean zero.
    """
    if not history_ok:
        return Viewing(
            status=HistoryStatus.UNAVAILABLE,
            notes=("history service did not return a usable response",),
        )

    keys, via = _match_rating_keys(film, crosswalk)

    distinct_targets = {(crosswalk[k].get("tmdb"), crosswalk[k].get("imdb")) for k in keys}
    ambiguous = via == "title-year" and len(distinct_targets) > 1

    best: dict[int, int] = {}
    plays = 0
    for key in keys:
        for row in rows_by_rating_key.get(key, []):
            plays += 1
            user_id = row.get("user_id")
            if user_id is None:
                continue
            percent = int(row.get("percent_complete") or 0)
            best[int(user_id)] = max(best.get(int(user_id), 0), percent)

    # Plays whose Plex item could not be identified. If one of them looks like
    # this film, its play count is not knowable.
    target_title = normalise_title(film.title)
    tainting = [
        row
        for row in (unattributed_rows or [])
        if normalise_title(row.get("title") or row.get("full_title")) == target_title
        and (
            film.year is None
            or not str(row.get("year", "")).isdigit()
            or abs(int(row["year"]) - film.year) <= 1
        )
    ]

    completions = tuple(
        Completion(user_id=uid, percent=pct)
        for uid, pct in sorted(best.items())
        if pct >= COMPLETION_PERCENT
    )

    notes: list[str] = []
    status = HistoryStatus.OK
    if ambiguous:
        status = HistoryStatus.AMBIGUOUS
        notes.append(f"{len(keys)} Plex items share this title/year; identity is not certain")
    elif tainting:
        status = HistoryStatus.UNRESOLVED
        notes.append(
            f"{len(tainting)} play(s) on a retired Plex item with this title could not be "
            "attributed by id"
        )
    elif coverage_start and availability.first_playable and availability.first_playable < coverage_start:
        status = HistoryStatus.INCOMPLETE
        notes.append(
            f"film was playable from {availability.first_playable.date()} but history only "
            f"starts {coverage_start.date()}"
        )
    elif not keys:
        notes.append("no play record of any kind; every history row resolved to another film")

    abandoned = [
        f"user {uid} reached {pct}%" for uid, pct in sorted(best.items()) if pct < ABANDONED_PERCENT
    ]
    if abandoned:
        notes.append("abandoned plays: " + ", ".join(abandoned))

    return Viewing(
        status=status,
        completions=completions,
        play_count=plays,
        identity_via=via if keys else "no-plays",
        rating_keys=tuple(sorted(keys)),
        notes=tuple(notes),
    )


def window_days(film: Film) -> int:
    """Grace period in days before a film may be considered for removal."""
    score = film.imdb_score
    if score is not None and score < 5.5:
        return 30
    return 90


def days_available(availability: Availability, now: datetime) -> int | None:
    """How long the film has actually been watchable."""
    if not availability.first_playable:
        return None
    return (now - availability.first_playable) // timedelta(days=1)
