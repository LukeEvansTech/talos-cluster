"""Turn raw API payloads into the three pieces of evidence a decision needs.

Pure functions only: every input is already-fetched data, so each rule here is
exercised from a fixture rather than from a live cluster.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from .clients import find_request
from .model import (
    DEFAULT_FEED_TAGS,
    Availability,
    EpisodeProgress,
    HistoryStatus,
    Origin,
    Provenance,
    Show,
    Viewing,
    normalise_title,
    utc,
)

# 85%+ counts as watched; below 20% is a bounce, not value; between is neither.
COMPLETION_PERCENT = 85
ABANDONED_PERCENT = 20

IMPORT_EVENT = "downloadFolderImported"

# Uncalibrated starting points: a season takes longer to get round to than a film.
WINDOW_DAYS = 180
SHORT_WINDOW_DAYS = 60
SHORT_WINDOW_BELOW_SCORE = 5.5


def resolve_provenance(
    show: Show,
    requests: dict[tuple[str, int], dict],
    feed_tags: Iterable[str] = DEFAULT_FEED_TAGS,
) -> Provenance:
    """Classify how a series entered the library.

    Two things count as evidence: a request record, or a tag a feed applies when
    it adds a show. Everything else is unknown. A record's creation date is not
    evidence -- it says nothing about who created it.
    """
    request = find_request(requests, show.tvdb_id, show.tmdb_id)
    if request:
        who = request.get("requested_by") or "unknown user"
        return Provenance(
            origin=Origin.REQUESTED,
            evidence=(f"request system: requested by {who} on {request.get('created_at', '?')}",),
            requested_by=who,
        )

    tags = sorted(show.tags & frozenset(feed_tags))
    if tags:
        return Provenance(
            origin=Origin.FEED,
            evidence=(f"feed tag(s) {', '.join(tags)} applied when the show was added",),
        )

    return Provenance(
        origin=Origin.UNKNOWN,
        evidence=("no request record and no feed tag",),
    )


def first_import_per_episode(history: list[dict]) -> dict[object, datetime]:
    """Each episode's earliest successful import.

    The earliest, so a quality upgrade -- which writes a fresh import event for
    the same episode -- cannot restart a window that has already run.
    """
    firsts: dict[object, datetime] = {}
    for index, row in enumerate(history):
        if row.get("eventType") != IMPORT_EVENT:
            continue
        stamp = utc(row.get("date"))
        if stamp is None:
            continue
        # A record with no episode id is still an import; keyed on its own position
        # so it can never merge with, or be hidden behind, another episode's.
        episode = row.get("episodeId") if row.get("episodeId") is not None else ("row", index)
        if episode not in firsts or stamp < firsts[episode]:
            firsts[episode] = stamp
    return firsts


def resolve_availability(
    show: Show,
    history: list[dict],
    history_horizon: datetime | None,
    ledger_first_seen: datetime | None,
    now: datetime,
) -> Availability:
    """Decide when a series became watchable and when its window starts.

    ``added`` is when Sonarr learned the series exists, which can precede any
    file by months. The window runs from imports instead. An ended show runs
    from its first import; a continuing one from the newest *episode's* first
    import, since a season that arrived last month is not something anybody has
    had a chance to watch.
    """
    firsts = first_import_per_episode(history)
    if firsts:
        series_first = min(firsts.values())
        newest_first = max(firsts.values())
        source = "sonarr-history"
        evidence = [f"first Sonarr import at {series_first.isoformat()}"]
        if ledger_first_seen and ledger_first_seen < series_first:
            # History aged out and an upgrade wrote a fresh import; the earlier
            # ledger observation wins so an already-run window can't restart.
            series_first = ledger_first_seen
            source = "ledger"
            evidence = [f"first observed playable at {ledger_first_seen.isoformat()}, earlier than retained imports"]
        if show.ended:
            window_start = series_first
            evidence.append("ended: window runs from the series' first import")
        else:
            window_start = max(newest_first, series_first)
            evidence.append(f"{show.status or 'not ended'}: window runs from the newest episode's first import")
        return Availability(
            first_playable=series_first,
            window_start=window_start,
            source=source,
            has_file=show.has_file,
            evidence=tuple(evidence),
        )

    if not show.has_file:
        return Availability(
            first_playable=None,
            source="none",
            has_file=False,
            evidence=("no episode on disk and no import event",),
        )

    # No import event: history aged out, or files arrived outside Sonarr. Use a
    # persisted observation if any, else start the clock now, delaying not cutting the window.
    if ledger_first_seen:
        return Availability(
            first_playable=ledger_first_seen,
            source="ledger",
            has_file=True,
            evidence=(f"first observed playable by this routine at {ledger_first_seen.isoformat()}",),
        )

    horizon = f" (Sonarr history only reaches {history_horizon.isoformat()})" if history_horizon else ""
    return Availability(
        first_playable=now,
        source="ledger-bootstrap",
        has_file=True,
        evidence=(f"no import event in retained history{horizon}; first observed now",),
    )


def _int_or_none(value) -> int | None:
    """An integer from a guid suffix or a numeric string, else None."""
    text = str(value) if value is not None else ""
    return int(text) if text.isdigit() else None


def build_crosswalk(metadata: dict[int, dict]) -> dict[int, dict]:
    """Map Plex show rating keys to external IDs from ``get_metadata`` payloads."""
    crosswalk: dict[int, dict] = {}
    for rating_key, meta in metadata.items():
        ids: dict[str, object] = {"tvdb": None, "tmdb": None, "imdb": None}
        for guid in meta.get("guids") or []:
            source, _, value = str(guid).partition("://")
            if source in ("tvdb", "tmdb"):
                ids[source] = _int_or_none(value)
            elif source == "imdb":
                ids["imdb"] = value or None
        crosswalk[int(rating_key)] = {
            **ids,
            "title": meta.get("title"),
            "year": _int_or_none(meta.get("year")),
        }
    return crosswalk


def _match_rating_keys(show: Show, crosswalk: dict[int, dict]) -> tuple[list[int], str]:
    """Find the Plex items that are this series, preferring stable identifiers."""
    for source, own in (("tvdb", show.tvdb_id), ("tmdb", show.tmdb_id), ("imdb", show.imdb_id)):
        if not own:
            continue
        found = [k for k, v in crosswalk.items() if v.get(source) == own]
        if found:
            return found, source

    # A fallback, not confirmation, since reboots share titles and years drift; a hit
    # is enough to refuse a deletion, not to confirm a match.
    target = normalise_title(show.title)
    by_title = [
        key
        for key, value in crosswalk.items()
        if not value.get("unresolved")
        and normalise_title(value.get("title")) == target
        and (show.year is None or value.get("year") is None or abs(value["year"] - show.year) <= 1)
    ]
    if by_title:
        return by_title, "title-year"
    return [], "unresolved"


def _episode_identity(row: dict):
    """What makes two rows the same episode, for counting distinct episodes."""
    season, episode = row.get("parent_media_index"), row.get("media_index")
    if str(season).isdigit() and str(episode).isdigit():
        return ("se", int(str(season)), int(str(episode)))
    return ("key", row.get("rating_key"), row.get("row_id"))


def _row_matches(row: dict, show: Show) -> bool:
    """Whether an unattributed play could belong to this series."""
    title = row.get("grandparent_title") or row.get("title") or row.get("full_title")
    if normalise_title(title) != normalise_title(show.title):
        return False
    year = row.get("year")
    return show.year is None or not str(year or "").isdigit() or abs(int(str(year)) - show.year) <= 1


def resolve_viewing(
    show: Show,
    rows_by_rating_key: dict[int, list[dict]],
    crosswalk: dict[int, dict],
    coverage_start: datetime | None,
    availability: Availability,
    history_ok: bool = True,
    unattributed_rows: list[dict] | None = None,
) -> Viewing:
    """Assemble play evidence for one series and say how far it can be trusted.

    Rows are keyed by the *show's* rating key (``grandparent_rating_key``). The
    join runs from history rows to series: a show nobody has played never appears
    in the history, so absence from the crosswalk is the expected state for
    exactly the shows this exists to remove.

    A zero is trustworthy only when the history was retrieved completely and
    every rating key in it resolved. Plays behind a retired key are matched back
    by title, and any series they might belong to goes to review.
    """
    if not history_ok:
        return Viewing(
            status=HistoryStatus.UNAVAILABLE,
            notes=("history service did not return a usable response",),
        )

    keys, via = _match_rating_keys(show, crosswalk)
    distinct_targets = {(crosswalk[k].get("tvdb"), crosswalk[k].get("tmdb"), crosswalk[k].get("imdb")) for k in keys}
    ambiguous = via == "title-year" and len(distinct_targets) > 1

    finished: dict[int, set] = {}
    best: dict[int, int] = {}
    plays = 0
    last: datetime | None = None
    for key in keys:
        for row in rows_by_rating_key.get(key, []):
            plays += 1
            stamp = row.get("stopped") or row.get("date")
            if str(stamp or "").isdigit():
                when = datetime.fromtimestamp(int(str(stamp)), tz=timezone.utc)
                last = when if last is None or when > last else last
            user_id = row.get("user_id")
            if user_id is None:
                continue
            percent = int(row.get("percent_complete") or 0)
            best[int(user_id)] = max(best.get(int(user_id), 0), percent)
            finished.setdefault(int(user_id), set())
            if percent >= COMPLETION_PERCENT:
                finished[int(user_id)].add(_episode_identity(row))

    tainting = [row for row in (unattributed_rows or []) if _row_matches(row, show)]
    progress = tuple(
        EpisodeProgress(user_id=uid, episodes=len(eps)) for uid, eps in sorted(finished.items()) if len(eps) >= 1
    )

    notes: list[str] = []
    status = HistoryStatus.OK
    if ambiguous:
        status = HistoryStatus.AMBIGUOUS
        notes.append(f"{len(keys)} Plex items share this title/year; identity is not certain")
    elif tainting:
        status = HistoryStatus.UNRESOLVED
        notes.append(f"{len(tainting)} play(s) on a retired Plex item with this title could not be attributed by id")
    elif coverage_start is None and availability.first_playable is not None:
        status = HistoryStatus.INCOMPLETE
        notes.append("play history carries no dated records, so its coverage cannot be established")
    elif coverage_start and availability.first_playable and availability.first_playable < coverage_start:
        status = HistoryStatus.INCOMPLETE
        notes.append(
            f"series was playable from {availability.first_playable.date()} but history only "
            f"starts {coverage_start.date()}"
        )
    elif not keys:
        notes.append("no play record of any kind; every history row resolved to another series")

    abandoned = [f"user {uid} reached {pct}%" for uid, pct in sorted(best.items()) if pct < ABANDONED_PERCENT]
    if abandoned:
        notes.append("abandoned plays: " + ", ".join(abandoned))

    return Viewing(
        status=status,
        progress=progress,
        play_count=plays,
        identity_via=via if keys else "no-plays",
        rating_keys=tuple(sorted(keys)),
        notes=tuple(notes),
        last_played=last,
    )


def window_days(show: Show) -> int:
    """Grace period in days before a series may be considered for removal."""
    score = show.imdb_score
    if score is not None and score < SHORT_WINDOW_BELOW_SCORE:
        return SHORT_WINDOW_DAYS
    return WINDOW_DAYS


def days_available(availability: Availability, now: datetime) -> int | None:
    """How long the series' window has actually been running."""
    if not availability.clock:
        return None
    return (now - availability.clock) // timedelta(days=1)
