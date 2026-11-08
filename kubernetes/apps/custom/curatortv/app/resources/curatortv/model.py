"""Value types shared across the engine.

Everything here is a plain dataclass with no I/O, so the decision modules can be
exercised entirely from fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Two legacy tags are absolute vetoes predating this engine. Three decision tags
# are set by a person in the Sonarr UI and outrank every automatic rule.

TAG_KEEP = "keep"
TAG_KEEP_REVIEW = "keep-review"
TAG_HUMAN_KEEP = "cleanup-keep"
TAG_HUMAN_DISMISSED = "cleanup-dismissed"
TAG_HUMAN_ELIGIBLE = "cleanup-eligible"

VETO_TAGS = frozenset({TAG_KEEP, TAG_KEEP_REVIEW, TAG_HUMAN_KEEP})
HUMAN_DECISION_TAGS = frozenset({TAG_HUMAN_KEEP, TAG_HUMAN_DISMISSED, TAG_HUMAN_ELIGIBLE})

# Sonarr has no import lists here; Kometa adds shows and tags them. Those tags are
# the only durable evidence that a feed, not a person, chose a title.
DEFAULT_FEED_TAGS = frozenset({"kometa"})


class Origin(str, Enum):
    """How a series came to be in the library."""

    FEED = "feed"
    REQUESTED = "requested"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    """What the engine concluded about a series, before Claude judges anything."""

    PROTECTED = "protected"  # a veto applies; never offered for judgement
    NOT_DUE = "not_due"  # in scope but still inside its viewing window
    MISSING_FILE = "missing_file"  # nothing to reclaim; belongs in its own report
    REVIEW = "review"  # cannot be judged safely; needs a person
    CANDIDATE = "candidate"  # eligible for Claude's judgement
    OUT_OF_SCOPE = "out_of_scope"


class HistoryStatus(str, Enum):
    """Why a play count can or cannot be trusted."""

    OK = "ok"  # complete history, identity resolved
    INCOMPLETE = "incomplete"  # history does not reach back far enough
    UNRESOLVED = "unresolved"  # could not map the series to a Plex item
    AMBIGUOUS = "ambiguous"  # more than one plausible Plex item
    UNAVAILABLE = "unavailable"  # Tautulli did not answer usably


def utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or None."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_title(title: str | None) -> str:
    """Lowercase and strip punctuation, for last-resort title matching."""
    return _NON_ALNUM.sub("", (title or "").lower())


@dataclass(frozen=True)
class Show:
    """A Sonarr series record, reduced to the fields any decision depends on."""

    series_id: int
    tvdb_id: int | None
    tmdb_id: int | None
    imdb_id: str | None
    title: str
    year: int | None
    status: str
    has_file: bool
    size_bytes: int
    added: datetime | None
    tags: frozenset[str]
    imdb_score: float | None
    imdb_votes: int | None
    genres: tuple[str, ...] = ()
    network: str | None = None
    overview: str = ""
    episode_file_count: int = 0
    season_count: int = 0

    @property
    def size_gb(self) -> float:
        """Size on disk in GB, for reporting and largest-first ordering."""
        return round(self.size_bytes / 1_000_000_000, 2)

    @property
    def ended(self) -> bool:
        """Whether no further episodes are expected."""
        return self.status == "ended"


@dataclass(frozen=True)
class Provenance:
    """Where a series came from, and the evidence for that claim."""

    origin: Origin
    evidence: tuple[str, ...] = ()
    requested_by: str | None = None


@dataclass(frozen=True)
class Availability:
    """When a series became watchable, and when its viewing window started.

    The two differ for a continuing show: ``first_playable`` is its first ever
    import, which bounds how far back play history must reach, while
    ``window_start`` is the newest episode's first import, because a new season
    is content nobody has yet had a chance to watch.
    """

    first_playable: datetime | None
    source: str  # sonarr-history | ledger | ledger-bootstrap | none
    has_file: bool
    evidence: tuple[str, ...] = ()
    window_start: datetime | None = None

    @property
    def clock(self) -> datetime | None:
        """The moment the viewing window runs from."""
        return self.window_start or self.first_playable


@dataclass(frozen=True)
class EpisodeProgress:
    """How many distinct episodes one person has finished."""

    user_id: int
    episodes: int


@dataclass(frozen=True)
class Viewing:
    """Play evidence for a series, plus an honest account of its limits."""

    status: HistoryStatus
    progress: tuple[EpisodeProgress, ...] = ()
    play_count: int = 0
    identity_via: str = "unresolved"
    rating_keys: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()
    last_played: datetime | None = None

    @property
    def distinct_viewers(self) -> int:
        """People who finished at least one episode."""
        return len({p.user_id for p in self.progress if p.episodes >= 1})

    @property
    def episodes_completed(self) -> int:
        """The most distinct episodes any one person has finished."""
        return max((p.episodes for p in self.progress), default=0)

    @property
    def trustworthy(self) -> bool:
        """True only when a zero here really means nobody watched it."""
        return self.status is HistoryStatus.OK


@dataclass
class Assessment:
    """The engine's verdict on one series."""

    show: Show
    outcome: Outcome
    provenance: Provenance
    availability: Availability
    viewing: Viewing
    window_days: int | None = None
    days_available: int | None = None
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    protections: list[str] = field(default_factory=list)
    # Without these the judge can't verify a subject-based keep reason.
    subject_counts: dict[str, int] = field(default_factory=dict)
    # Set when the planner reopened a dismissal because its facts changed; the executor
    # then vetoes only a dismissal added after the plan.
    reopened_dismissal: bool = False

    def to_json(self) -> dict[str, Any]:
        """Serialise for the candidate hand-off and the run report."""
        show, availability, viewing = self.show, self.availability, self.viewing
        return {
            "series_id": show.series_id,
            "tvdb_id": show.tvdb_id,
            "tmdb_id": show.tmdb_id,
            "imdb_id": show.imdb_id,
            "title": show.title,
            "year": show.year,
            "status": show.status,
            "size_gb": show.size_gb,
            "season_count": show.season_count,
            "episode_file_count": show.episode_file_count,
            "outcome": self.outcome.value,
            "origin": self.provenance.origin.value,
            "origin_evidence": list(self.provenance.evidence),
            "requested_by": self.provenance.requested_by,
            "first_playable": (availability.first_playable.isoformat() if availability.first_playable else None),
            "window_start": availability.clock.isoformat() if availability.clock else None,
            "availability_source": availability.source,
            "days_available": self.days_available,
            "window_days": self.window_days,
            "imdb_score": show.imdb_score,
            "imdb_votes": show.imdb_votes,
            "subject_counts": dict(self.subject_counts),
            "reopened_dismissal": TAG_HUMAN_DISMISSED in self.show.tags,
            "genres": list(show.genres),
            "network": show.network,
            # The judge reads this field and injection handling is written against
            # it; omit it and both go silently inert.
            "overview": show.overview,
            "history_status": viewing.status.value,
            "identity_via": viewing.identity_via,
            "play_count": viewing.play_count,
            "distinct_viewers": viewing.distinct_viewers,
            "episodes_completed": viewing.episodes_completed,
            "last_played": viewing.last_played.isoformat() if viewing.last_played else None,
            "rating_keys": list(viewing.rating_keys),
            "viewers": [{"user_id": p.user_id, "episodes": p.episodes} for p in viewing.progress],
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "protections": list(self.protections),
        }
