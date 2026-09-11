"""Scope, protection, batching and anomaly rules.

This is the half of the routine that must never be wrong, so every rule is a pure
function over already-fetched data and every one of them is covered by a fixture.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from .evidence import days_available, window_days
from .model import (
    TAG_HUMAN_DISMISSED,
    TAG_HUMAN_ELIGIBLE,
    TAG_KEEP,
    TAG_KEEP_REVIEW,
    VETO_TAGS,
    Assessment,
    Availability,
    Film,
    HistoryStatus,
    Origin,
    Outcome,
    Provenance,
    Viewing,
)

# Batch rails.
MAX_DELETIONS_PER_RUN = 30
# How long a spare stands before the same film is judged again. Long enough that
# a weekly run is not re-litigating the same 500 films, short enough that a
# changed fact is picked up within a season.
DEFAULT_RECONSIDER_DAYS = 180

# Anomaly thresholds. Each one answers "what shrank so much that something is
# broken rather than merely different?"
ANOMALY_LIMITS = {
    "library_size_drop_pct": 10.0,
    "keep_tag_drop_pct": 2.0,
    "exclusion_drop_pct": 5.0,
    "history_rows_drop_pct": 20.0,
    "invalid_added_pct": 1.0,
}


@dataclass
class PlanContext:
    """Everything the planner needs that is not the film itself."""

    now: datetime
    scope_start: datetime
    monitored_collection_tmdb_ids: frozenset[int]
    coverage_start: datetime | None
    history_ok: bool
    decisions: dict[int, dict] = field(default_factory=dict)
    collections_loaded: bool = True


def fact_fingerprint(
    film: Film, provenance: Provenance, viewing: Viewing, in_monitored_collection: bool
) -> str:
    """Hash the facts a spare decision rested on.

    A spare is allowed to stand until one of these moves. Scores are bucketed so
    an IMDb rating drifting by 0.1 does not re-open a settled judgement, while a
    real change -- a new completion, a collection becoming monitored, the film
    gaining a provenance tag -- does.
    """
    payload = {
        "score_bucket": None if film.imdb_score is None else round(film.imdb_score * 2) / 2,
        "votes_bucket": None if film.imdb_votes is None else film.imdb_votes // 1000,
        "completers": viewing.distinct_completers,
        "origin": provenance.origin.value,
        "monitored_collection": in_monitored_collection,
        "collection": film.collection_title,
        "has_file": film.has_file,
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def decision_still_binding(decision: dict, fingerprint: str, now: datetime) -> bool:
    """True when a stored spare/dismissal still applies to today's facts."""
    if decision.get("fact_fingerprint") != fingerprint:
        return False
    after = decision.get("reconsider_after")
    if after:
        try:
            if now >= datetime.fromisoformat(after.replace("Z", "+00:00")):
                return False
        except ValueError:
            return False
    return True


def in_monitored_collection(film: Film, ctx: PlanContext) -> bool:
    """Membership of a collection Radarr has been told to complete."""
    return bool(film.tmdb_id and film.tmdb_id in ctx.monitored_collection_tmdb_ids)


def assess(
    film: Film,
    provenance: Provenance,
    availability: Availability,
    viewing: Viewing,
    ctx: PlanContext,
) -> Assessment:
    """Decide what may be done with one film, before any question of taste.

    Order matters: protections are evaluated before eligibility, so a film that
    is both protected and technically past its window is reported as protected.
    """
    monitored = in_monitored_collection(film, ctx)
    window = window_days(film)
    available_for = days_available(availability, ctx.now)
    result = Assessment(
        film=film,
        outcome=Outcome.REVIEW,
        provenance=provenance,
        availability=availability,
        viewing=viewing,
        window_days=window,
        days_available=available_for,
    )

    # --- absolute vetoes ---------------------------------------------------
    vetoes = sorted(film.tags & VETO_TAGS)
    if vetoes:
        result.outcome = Outcome.PROTECTED
        result.protections.append(f"carries {', '.join(vetoes)}")
        return result

    if monitored:
        result.outcome = Outcome.PROTECTED
        result.protections.append(
            "belongs to a monitored collection; completing that set is a standing instruction"
        )
        return result

    if not ctx.collections_loaded:
        result.outcome = Outcome.REVIEW
        result.blockers.append("collection data unavailable, so collection protection cannot be checked")
        return result

    if provenance.origin is Origin.REQUESTED:
        result.outcome = Outcome.PROTECTED
        result.protections.append(
            f"explicitly requested by {provenance.requested_by or 'a household member'}"
        )
        return result

    # --- scope -------------------------------------------------------------
    if film.status != "released":
        result.outcome = Outcome.OUT_OF_SCOPE
        result.reasons.append(f"status is {film.status}, not released")
        return result

    if film.added is None:
        result.outcome = Outcome.REVIEW
        result.blockers.append("record has no usable added date")
        return result

    if film.added < ctx.scope_start:
        result.outcome = Outcome.OUT_OF_SCOPE
        result.reasons.append(
            f"added {film.added.date()}, before the {ctx.scope_start.date()} scope boundary"
        )
        return result

    # --- availability ------------------------------------------------------
    if not film.has_file:
        result.outcome = Outcome.MISSING_FILE
        result.reasons.append("no file on disk; nothing to reclaim and nobody could have watched it")
        return result

    if available_for is None:
        result.outcome = Outcome.REVIEW
        result.blockers.append("cannot establish when the film became playable")
        return result

    if available_for < window:
        result.outcome = Outcome.NOT_DUE
        result.reasons.append(
            f"playable for {available_for} of {window} days"
        )
        return result

    # --- viewing evidence --------------------------------------------------
    if viewing.status is HistoryStatus.UNAVAILABLE:
        result.outcome = Outcome.REVIEW
        result.blockers.append("play history unavailable; a zero here would not mean unwatched")
        return result

    if viewing.status in (HistoryStatus.INCOMPLETE, HistoryStatus.UNRESOLVED, HistoryStatus.AMBIGUOUS):
        result.outcome = Outcome.REVIEW
        result.blockers.append(f"play history {viewing.status.value}: " + "; ".join(viewing.notes))
        return result

    if viewing.distinct_completers >= 2:
        result.outcome = Outcome.REVIEW
        result.reasons.append(
            f"{viewing.distinct_completers} distinct users finished it; belongs in the human queue"
        )
        return result

    # --- authorisation -----------------------------------------------------
    # Delete-by-default needs an authorisation, and there are exactly two:
    # established feed provenance, or an explicit human decision. A human tag is
    # a PARALLEL authorisation, not a way of relabelling an unknown as a feed
    # pull -- the origin below still reports "unknown" in the evidence.
    authorised_by = None
    if provenance.origin is Origin.FEED:
        authorised_by = "import-list provenance"
    elif TAG_HUMAN_ELIGIBLE in film.tags:
        authorised_by = f"human decision tag {TAG_HUMAN_ELIGIBLE}"

    if not authorised_by:
        result.outcome = Outcome.REVIEW
        result.blockers.append(
            "origin unknown: no request record and no import-list provenance tag, so "
            "delete-by-default is not authorised for this film"
        )
        return result

    # --- standing human decisions -----------------------------------------
    fingerprint = fact_fingerprint(film, provenance, viewing, monitored)
    stored = ctx.decisions.get(film.movie_id)
    if stored and decision_still_binding(stored, fingerprint, ctx.now):
        result.outcome = Outcome.REVIEW
        verdict = stored.get("verdict", "spared")
        result.reasons.append(
            f"already {verdict} on {stored.get('decided_at', '?')[:10]}: "
            f"{stored.get('reason', 'no reason recorded')}"
        )
        return result

    if TAG_HUMAN_DISMISSED in film.tags and stored and stored.get("fact_fingerprint") == fingerprint:
        result.outcome = Outcome.REVIEW
        result.reasons.append("dismissed by a person and no relevant fact has changed since")
        return result

    result.outcome = Outcome.CANDIDATE
    result.reasons.append(f"eligible: authorised by {authorised_by}, past its {window}-day window")
    return result


# --- allow-list maintenance ----------------------------------------------

KEEP_BAR_VOTES = 5000
KEEP_BAR_SCORE = 6.5


def keep_tag_targets(
    films: Iterable[Film], bar_votes: int = KEEP_BAR_VOTES, bar_score: float = KEEP_BAR_SCORE
) -> list[Film]:
    """Films that should gain the ``keep`` tag this run.

    Add-only, and with two exclusions that stop automatic maintenance quietly
    overturning a person:

    * a ``keep-review`` item is never promoted -- the whole point of that queue is
      that a human decides, and applying ``keep`` decides it for them;
    * a film a person marked ``cleanup-eligible`` is never re-protected, or the
      rating rule silently reverses their override every single week.
    """
    targets = []
    for film in films:
        if TAG_KEEP in film.tags or TAG_KEEP_REVIEW in film.tags:
            continue
        if TAG_HUMAN_ELIGIBLE in film.tags or TAG_HUMAN_DISMISSED in film.tags:
            continue
        if film.imdb_votes is None or film.imdb_score is None:
            continue
        if film.imdb_votes >= bar_votes and film.imdb_score >= bar_score:
            targets.append(film)
    return targets


# --- batching -------------------------------------------------------------


def select_batch(
    approved: list[Assessment],
    cap: int = MAX_DELETIONS_PER_RUN,
    already_deleted_this_window: int = 0,
) -> tuple[list[Assessment], list[Assessment]]:
    """Take the largest files first, up to what remains of the run's budget.

    There is deliberately no "too many candidates, abort everything" rule. That
    rule counts spares as if they were deletions, and it can never drain a
    backlog larger than its own threshold -- it stalls permanently, re-finding
    the same films every week. Anomaly detection, not volume, is what stops a run.
    """
    remaining = max(cap - already_deleted_this_window, 0)
    ordered = sorted(approved, key=lambda a: a.film.size_bytes, reverse=True)
    return ordered[:remaining], ordered[remaining:]


# --- anomaly detection ----------------------------------------------------


def _drop_pct(current: float, baseline: float) -> float:
    """Percentage fall from baseline to current; 0 when baseline is empty."""
    if baseline <= 0:
        return 0.0
    return max(0.0, (baseline - current) / baseline * 100.0)


def snapshot_valid(snapshot: dict[str, Any]) -> list[str]:
    """Reject a snapshot that is too malformed to be a baseline.

    A baseline recorded from a half-fetched run becomes the yardstick every later
    run is measured against, so a bad one disables anomaly detection silently and
    permanently. Refusing to record is always the safer failure.
    """
    problems = []
    for key in ("library_size", "keep_tag_count", "collection_count", "exclusion_count"):
        value = snapshot.get(key)
        if not isinstance(value, int) or value < 0:
            problems.append(f"{key} missing or not a non-negative integer")
    if isinstance(snapshot.get("library_size"), int) and snapshot["library_size"] == 0:
        problems.append("library_size is zero, which is never a valid baseline here")
    if not snapshot.get("history_ok", False):
        problems.append("history service was not healthy during this snapshot")
    return problems


def check_anomalies(snapshot: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    """Compare this run against the last validated baseline.

    Returns the reasons deletion must not proceed. An empty list means the run
    looks ordinary.
    """
    if baseline is None:
        return ["no validated baseline yet; recording one and deleting nothing this run"]

    blocks: list[str] = []
    checks = (
        ("library_size", "library_size_drop_pct", "library size"),
        ("keep_tag_count", "keep_tag_drop_pct", "keep-tagged films"),
        ("exclusion_count", "exclusion_drop_pct", "import exclusions"),
        ("history_rows", "history_rows_drop_pct", "play-history rows"),
    )
    for key, limit_key, label in checks:
        current = snapshot.get(key)
        previous = baseline.get(key)
        if not isinstance(current, int) or not isinstance(previous, int):
            continue
        drop = _drop_pct(current, previous)
        if drop > ANOMALY_LIMITS[limit_key]:
            blocks.append(
                f"{label} fell {drop:.1f}% ({previous} -> {current}), over the "
                f"{ANOMALY_LIMITS[limit_key]}% limit"
            )

    if baseline.get("monitored_collections", 0) > 0 and snapshot.get("monitored_collections", 0) == 0:
        blocks.append("every collection lost its monitored flag since the last run")

    invalid = snapshot.get("invalid_added_count", 0)
    size = snapshot.get("library_size", 0) or 1
    if invalid / size * 100.0 > ANOMALY_LIMITS["invalid_added_pct"]:
        blocks.append(f"{invalid} films have an unusable added date")

    if not snapshot.get("collections_loaded", True):
        blocks.append("collection data did not load, so collection protection cannot be enforced")

    return blocks


def build_decision_record(
    assessment: Assessment,
    verdict: str,
    reason: str,
    run_id: str,
    now: datetime,
    monitored: bool,
    reconsider_days: int = DEFAULT_RECONSIDER_DAYS,
) -> dict[str, Any]:
    """A spare or dismissal, with the facts it rested on and when to revisit."""
    return {
        "movie_id": assessment.film.movie_id,
        "tmdb_id": assessment.film.tmdb_id,
        "title": assessment.film.title,
        "verdict": verdict,
        "reason": reason,
        "decided_at": now.isoformat(),
        "run_id": run_id,
        "reconsider_after": (now + timedelta(days=reconsider_days)).isoformat(),
        "fact_fingerprint": fact_fingerprint(
            assessment.film, assessment.provenance, assessment.viewing, monitored
        ),
    }


def summarise(assessments: Iterable[Assessment]) -> dict[str, int]:
    """Count assessments by outcome, for the report and the shape check."""
    counts: dict[str, int] = {}
    for item in assessments:
        counts[item.outcome.value] = counts.get(item.outcome.value, 0) + 1
    return counts
