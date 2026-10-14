"""Builders and in-memory doubles, so no test needs a live cluster."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from curatortv.model import (
    Availability,
    EpisodeProgress,
    HistoryStatus,
    Origin,
    Provenance,
    Show,
    Viewing,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
SCOPE_START = datetime(2026, 3, 1, tzinfo=timezone.utc)


def days_ago(count: int) -> datetime:
    """A timestamp ``count`` days before the fixed test clock."""
    return NOW - timedelta(days=count)


def make_show(**overrides) -> Show:
    """An ended, feed-tagged, unprotected series past its window by default."""
    base: dict[str, Any] = {
        "series_id": 1,
        "tvdb_id": 70001,
        "tmdb_id": 1000,
        "imdb_id": "tt0000001",
        "title": "Test Show",
        "year": 2026,
        "status": "ended",
        "has_file": True,
        "size_bytes": 50_000_000_000,
        "added": days_ago(190),
        "tags": frozenset({"kometa"}),
        "imdb_score": 6.0,
        "imdb_votes": 4000,
        "episode_file_count": 10,
        "season_count": 1,
    }
    base.update(overrides)
    if isinstance(base["tags"], (set, list, tuple)):
        base["tags"] = frozenset(base["tags"])
    return Show(**base)


def make_availability(days: int = 185, source: str = "sonarr-history", window_days: int | None = None) -> Availability:
    """Availability that started ``days`` ago, its window ``window_days`` ago."""
    return Availability(
        first_playable=days_ago(days),
        source=source,
        has_file=True,
        window_start=days_ago(window_days if window_days is not None else days),
    )


def make_viewing(viewers: int = 0, status: HistoryStatus = HistoryStatus.OK, plays: int = 0, episodes: int = 3):
    """Viewing evidence with ``viewers`` distinct people who finished episodes."""
    return Viewing(
        status=status,
        progress=tuple(EpisodeProgress(user_id=100 + i, episodes=episodes) for i in range(viewers)),
        play_count=plays or viewers * episodes,
        identity_via="tvdb",
        rating_keys=(5000,),
    )


def feed_provenance() -> Provenance:
    """Provenance proving a feed added the series."""
    return Provenance(origin=Origin.FEED, evidence=("feed tag kometa",))


def unknown_provenance() -> Provenance:
    """Provenance that establishes nothing."""
    return Provenance(origin=Origin.UNKNOWN, evidence=("no request record and no feed tag",))


def import_event(when: datetime, episode_id: int = 1, **extra) -> dict:
    """A Sonarr ``downloadFolderImported`` history row for one episode."""
    return {"eventType": "downloadFolderImported", "date": when.isoformat(), "episodeId": episode_id, **extra}


def play_row(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    show_key: int,
    user_id: int,
    percent: int,
    row_id: int,
    season: int = 1,
    episode: int = 1,
    date: int = 1_780_000_000,
) -> dict:
    """One ungrouped Tautulli episode history row."""
    return {
        "row_id": row_id,
        "id": row_id,
        "rating_key": show_key * 1000 + season * 100 + episode,
        "parent_rating_key": show_key * 1000 + season,
        "grandparent_rating_key": show_key,
        "grandparent_title": "Test Show",
        "parent_media_index": season,
        "media_index": episode,
        "user_id": user_id,
        "percent_complete": percent,
        "date": date,
        "year": 2026,
        "media_type": "episode",
    }


def series_record(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    series_id=1,
    tags=None,
    tvdb_id=70001,
    status="ended",
    episode_files=10,
    tmdb_id=1000,
    imdb_id="tt0000001",
) -> dict:
    """A minimal live Sonarr series record."""
    return {
        "id": series_id,
        "tvdbId": tvdb_id,
        "tmdbId": tmdb_id,
        "imdbId": imdb_id,
        "title": "Test Show",
        "year": 2026,
        "status": status,
        "added": "2026-03-10T00:00:00Z",
        "tags": tags or [],
        "ratings": {"value": 6.0, "votes": 4000},
        "statistics": {
            "episodeFileCount": episode_files,
            "episodeCount": 10,
            "totalEpisodeCount": 10,
            "sizeOnDisk": 50_000_000_000,
            "seasonCount": 1,
        },
        "seasons": [{"seasonNumber": 1, "monitored": True}],
    }


class FakeS3:
    """An in-memory stand-in for the S3 client, with the same surface."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.put_calls = 0

    def get(self, key: str):
        """Object body or None."""
        return self.store.get(key)

    def put(self, key: str, body: bytes) -> None:
        """Store an object."""
        self.put_calls += 1
        self.store[key] = body

    def delete(self, key: str) -> None:
        """Remove an object."""
        self.store.pop(key, None)

    def list_keys(self, prefix: str, limit: int = 1000) -> list[str]:
        """Keys under a prefix; ``limit`` mirrors the real client's signature."""
        del limit
        return sorted(k for k in self.store if k.startswith(prefix))

    def loads(self, key: str):
        """Decode a stored object, for assertions."""
        return json.loads(self.store[key])


class FakeSonarr:  # pylint: disable=too-many-instance-attributes
    """A Sonarr double that records writes and can simulate failures."""

    def __init__(self, series: dict[int, dict], tags: dict[int, str] | None = None):
        self.series_by_id = dict(series)
        self.tags_by_id = tags or {}
        self.deleted: list[int] = []
        self.delete_calls: list[int] = []
        self.tag_calls: list[tuple[list[int], int]] = []
        self.delete_behaviour: dict[int, object] = {}
        self.read_errors: dict[int, Exception] = {}
        self.read_errors_after: dict[int, tuple[int, Exception]] = {}
        self.reads: dict[int, int] = {}
        self.media_config: dict = {
            "recycleBin": "/recycle",
            "recycleBinCleanupDays": 14,
        }

    def series(self, series_id: int):
        """One live series record, or None once deleted.

        ``read_errors`` raises on every read; ``read_errors_after`` raises only
        once that many reads have succeeded, which is how a failure *after* the
        delete is modelled rather than one before it.
        """
        self.reads[series_id] = self.reads.get(series_id, 0) + 1
        if series_id in self.read_errors:
            raise self.read_errors[series_id]
        after = self.read_errors_after.get(series_id)
        if after is not None and self.reads[series_id] > after[0]:
            raise after[1]
        return self.series_by_id.get(series_id)

    def media_management(self) -> dict:
        """Recycle-bin configuration."""
        return self.media_config

    def all_series(self) -> list[dict]:
        """The whole library."""
        return list(self.series_by_id.values())

    def tags(self) -> list[dict]:
        """Tag definitions."""
        return [{"id": i, "label": label} for i, label in self.tags_by_id.items()]

    def create_tag(self, label: str):
        """Create a tag with the next free id."""
        new_id = max(self.tags_by_id, default=0) + 1
        self.tags_by_id[new_id] = label
        return 201, {"id": new_id, "label": label}

    def add_tag(self, series_ids: list[int], tag_id: int):
        """Apply a tag the way the editor endpoint does, and record the call."""
        self.tag_calls.append((list(series_ids), tag_id))
        for series_id in series_ids:
            record = self.series_by_id.get(series_id)
            if record is not None:
                record["tags"] = sorted(set(record.get("tags") or []) | {tag_id})
        return 202, None

    def delete_series(self, series_id: int, add_exclusion: bool = True):  # pylint: disable=unused-argument
        """Delete, honouring any behaviour injected for this id."""
        self.delete_calls.append(series_id)
        behaviour = self.delete_behaviour.get(series_id)
        if isinstance(behaviour, Exception):
            if getattr(behaviour, "applied", False):
                self.series_by_id.pop(series_id, None)
                self.deleted.append(series_id)
            raise behaviour
        if behaviour == "http-500":
            return 500, None
        if behaviour == "silent-noop":
            return 200, None  # answers OK but the record survives
        self.series_by_id.pop(series_id, None)
        self.deleted.append(series_id)
        return 200, None


class FakeTautulli:
    """A Tautulli double for the activity check."""

    def __init__(
        self,
        raise_error: Exception | None = None,
        playing_ids: set[tuple[str, str]] | None = None,
    ):
        self.raise_error = raise_error
        self.playing_ids = playing_ids or set()
        # Successive answers, for modelling somebody pressing play midway
        # through a batch. The last entry repeats once the list runs out.
        self.playing_ids_sequence: list[set[tuple[str, str]]] | None = None
        self.now_playing_calls = 0
        self.history_rows: list[dict] = []
        self.history_complete = True

    def now_playing_ids(self) -> set[tuple[str, str]]:
        """External ids currently streaming."""
        if self.raise_error:
            raise self.raise_error
        self.now_playing_calls += 1
        if self.playing_ids_sequence:
            index = min(self.now_playing_calls - 1, len(self.playing_ids_sequence) - 1)
            return self.playing_ids_sequence[index]
        return self.playing_ids

    def episode_history(self):
        """Play history, re-read at execution time to catch late completions."""
        if self.raise_error:
            raise self.raise_error
        return self.history_rows, {"complete": self.history_complete}
