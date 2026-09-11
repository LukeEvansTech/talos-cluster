"""Builders and in-memory doubles, so no test needs a live cluster."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from curator.model import (
    Availability,
    Completion,
    Film,
    HistoryStatus,
    Origin,
    Provenance,
    Viewing,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
SCOPE_START = datetime(2026, 3, 1, tzinfo=timezone.utc)


def days_ago(count: int) -> datetime:
    """A timestamp ``count`` days before the fixed test clock."""
    return NOW - timedelta(days=count)


def make_film(**overrides) -> Film:
    """A released, feed-tagged, unprotected film past its window by default."""
    base: dict[str, Any] = {
        "movie_id": 1,
        "tmdb_id": 1000,
        "imdb_id": "tt0000001",
        "title": "Test Film",
        "year": 2026,
        "status": "released",
        "has_file": True,
        "size_bytes": 10_000_000_000,
        "added": days_ago(120),
        "tags": frozenset({"src-tmdb-popular"}),
        "imdb_score": 6.0,
        "imdb_votes": 4000,
    }
    base.update(overrides)
    if isinstance(base["tags"], (set, list, tuple)):
        base["tags"] = frozenset(base["tags"])
    return Film(**base)


def make_availability(days: int = 120, source: str = "radarr-history") -> Availability:
    """Availability that started ``days`` ago."""
    return Availability(first_playable=days_ago(days), source=source, has_file=True)


def make_viewing(completers: int = 0, status: HistoryStatus = HistoryStatus.OK, plays: int = 0) -> Viewing:
    """Viewing evidence with ``completers`` distinct finishers."""
    return Viewing(
        status=status,
        completions=tuple(Completion(user_id=100 + i, percent=95) for i in range(completers)),
        play_count=plays or completers,
        identity_via="tmdb",
        rating_keys=(5000,),
    )


def feed_provenance() -> Provenance:
    """Provenance proving an import list added the film."""
    return Provenance(origin=Origin.FEED, evidence=("provenance tag src-tmdb-popular",))


def unknown_provenance() -> Provenance:
    """Provenance that establishes nothing."""
    return Provenance(origin=Origin.UNKNOWN, evidence=("no request record and no provenance tag",))


def import_event(when: datetime, **extra) -> dict:
    """A Radarr ``downloadFolderImported`` history row."""
    return {"eventType": "downloadFolderImported", "date": when.isoformat(), **extra}


def play_row(rating_key: int, user_id: int, percent: int, row_id: int, date: int = 1_780_000_000) -> dict:
    """One ungrouped Tautulli history row."""
    return {
        "row_id": row_id,
        "id": row_id,
        "rating_key": rating_key,
        "user_id": user_id,
        "percent_complete": percent,
        "date": date,
        "media_type": "movie",
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


class FakeRadarr:
    """A Radarr double that records writes and can simulate failures."""

    def __init__(self, movies: dict[int, dict], tags: dict[int, str] | None = None):
        self.movies_by_id = dict(movies)
        self.tags_by_id = tags or {}
        self.deleted: list[int] = []
        self.delete_behaviour: dict[int, object] = {}
        self.read_errors: dict[int, Exception] = {}
        self.read_errors_after: dict[int, tuple[int, Exception]] = {}
        self.reads: dict[int, int] = {}
        self.media_config: dict = {
            "recycleBin": "/recycle",
            "recycleBinCleanupDays": 14,
        }

    def movie(self, movie_id: int):
        """One live movie record, or None once deleted.

        ``read_errors`` raises on every read; ``read_errors_after`` raises only
        once that many reads have succeeded, which is how a failure *after* the
        delete is modelled rather than one before it.
        """
        self.reads[movie_id] = self.reads.get(movie_id, 0) + 1
        if movie_id in self.read_errors:
            raise self.read_errors[movie_id]
        after = self.read_errors_after.get(movie_id)
        if after is not None and self.reads[movie_id] > after[0]:
            raise after[1]
        return self.movies_by_id.get(movie_id)

    def media_management(self) -> dict:
        """Recycle-bin configuration."""
        return self.media_config

    def movies(self) -> list[dict]:
        """The whole library."""
        return list(self.movies_by_id.values())

    def tags(self) -> list[dict]:
        """Tag definitions."""
        return [{"id": i, "label": label} for i, label in self.tags_by_id.items()]

    def delete_movie(self, movie_id: int, add_exclusion: bool = True):  # pylint: disable=unused-argument
        """Delete, honouring any behaviour injected for this id."""
        behaviour = self.delete_behaviour.get(movie_id)
        if isinstance(behaviour, Exception):
            if getattr(behaviour, "applied", False):
                self.movies_by_id.pop(movie_id, None)
                self.deleted.append(movie_id)
            raise behaviour
        if behaviour == "http-500":
            return 500, None
        if behaviour == "silent-noop":
            return 200, None  # answers OK but the record survives
        self.movies_by_id.pop(movie_id, None)
        self.deleted.append(movie_id)
        return 200, None


class FakeTautulli:
    """A Tautulli double for the activity check."""

    def __init__(
        self,
        playing: set[int] | None = None,
        raise_error: Exception | None = None,
        playing_ids: set[tuple[str, str]] | None = None,
    ):
        self.playing = playing or set()
        self.raise_error = raise_error
        self.playing_ids = playing_ids or set()

    def now_playing_rating_keys(self) -> set[int]:
        """Rating keys currently streaming."""
        if self.raise_error:
            raise self.raise_error
        return self.playing

    def now_playing_ids(self) -> set[tuple[str, str]]:
        """External ids currently streaming."""
        if self.raise_error:
            raise self.raise_error
        return self.playing_ids
