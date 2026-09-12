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

# --- Tag vocabulary -------------------------------------------------------
# Two legacy tags are absolute vetoes and predate this engine. Three decision
# tags are set by a person in the Radarr UI and outrank every automatic rule.

TAG_KEEP = "keep"
TAG_KEEP_REVIEW = "keep-review"
TAG_HUMAN_KEEP = "cleanup-keep"
TAG_HUMAN_DISMISSED = "cleanup-dismissed"
TAG_HUMAN_ELIGIBLE = "cleanup-eligible"

VETO_TAGS = frozenset({TAG_KEEP, TAG_KEEP_REVIEW, TAG_HUMAN_KEEP})
HUMAN_DECISION_TAGS = frozenset({TAG_HUMAN_KEEP, TAG_HUMAN_DISMISSED, TAG_HUMAN_ELIGIBLE})

# Provenance tags are applied by Radarr itself when an import list adds a film.
# They are the only durable evidence that a feed, not a person, chose a title.
PROVENANCE_TAG_PREFIX = "src-"


class Origin(str, Enum):
    """How a film came to be in the library."""

    FEED = "feed"
    REQUESTED = "requested"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    """What the engine concluded about a film, before Claude judges anything."""

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
    UNRESOLVED = "unresolved"  # could not map the film to a Plex item
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
class Film:
    """A Radarr movie record, reduced to the fields any decision depends on."""

    movie_id: int
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
    collection_title: str | None = None
    genres: tuple[str, ...] = ()
    studio: str | None = None
    original_language: str | None = None
    overview: str = ""
    file_date_added: datetime | None = None

    @property
    def size_gb(self) -> float:
        """Size on disk in GB, for reporting and largest-first ordering."""
        return round(self.size_bytes / 1_000_000_000, 2)

    @property
    def provenance_tags(self) -> frozenset[str]:
        """Import-list provenance tags carried by this film."""
        return frozenset(t for t in self.tags if t.startswith(PROVENANCE_TAG_PREFIX))


@dataclass(frozen=True)
class Provenance:
    """Where a film came from, and the evidence for that claim."""

    origin: Origin
    evidence: tuple[str, ...] = ()
    requested_by: str | None = None


@dataclass(frozen=True)
class Availability:
    """When a film first became watchable, and how confidently we know."""

    first_playable: datetime | None
    source: str  # radarr-history | ledger | none
    has_file: bool
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Completion:
    """One user's furthest progress through a film."""

    user_id: int
    percent: int


@dataclass(frozen=True)
class Viewing:
    """Play evidence for a film, plus an honest account of its limits."""

    status: HistoryStatus
    completions: tuple[Completion, ...] = ()
    play_count: int = 0
    identity_via: str = "unresolved"
    rating_keys: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def distinct_completers(self) -> int:
        """Number of distinct users who finished the film."""
        return len({c.user_id for c in self.completions})

    @property
    def trustworthy(self) -> bool:
        """True only when a zero here really means nobody watched it."""
        return self.status is HistoryStatus.OK


@dataclass
class Assessment:
    """The engine's verdict on one film."""

    film: Film
    outcome: Outcome
    provenance: Provenance
    availability: Availability
    viewing: Viewing
    window_days: int | None = None
    days_available: int | None = None
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    protections: list[str] = field(default_factory=list)
    # Evidence the keep reasons are stated in terms of. Without these the judge
    # is asked to prove a collection is partly owned, or that the library
    # follows a subject, from information it was never given -- and a rule that
    # defaults to deletion resolves that absence as "delete".
    siblings_owned: int = 0
    subject_counts: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Serialise for the candidate hand-off and the run report."""
        return {
            "movie_id": self.film.movie_id,
            "tmdb_id": self.film.tmdb_id,
            "imdb_id": self.film.imdb_id,
            "title": self.film.title,
            "year": self.film.year,
            "size_gb": self.film.size_gb,
            "outcome": self.outcome.value,
            "origin": self.provenance.origin.value,
            "origin_evidence": list(self.provenance.evidence),
            "requested_by": self.provenance.requested_by,
            "first_playable": (
                self.availability.first_playable.isoformat() if self.availability.first_playable else None
            ),
            "availability_source": self.availability.source,
            "days_available": self.days_available,
            "window_days": self.window_days,
            "imdb_score": self.film.imdb_score,
            "imdb_votes": self.film.imdb_votes,
            "collection": self.film.collection_title,
            "siblings_owned": self.siblings_owned,
            "subject_counts": dict(self.subject_counts),
            "genres": list(self.film.genres),
            "studio": self.film.studio,
            "history_status": self.viewing.status.value,
            "identity_via": self.viewing.identity_via,
            "play_count": self.viewing.play_count,
            "distinct_completers": self.viewing.distinct_completers,
            "rating_keys": list(self.viewing.rating_keys),
            "completions": [{"user_id": c.user_id, "percent": c.percent} for c in self.viewing.completions],
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "protections": list(self.protections),
        }
