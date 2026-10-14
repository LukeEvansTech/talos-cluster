"""Scope, protection, batching and anomaly rules.

Pure functions over already-fetched data, so every rule is covered by a fixture.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .evidence import days_available, window_days
from .model import (
    TAG_HUMAN_DISMISSED,
    TAG_HUMAN_ELIGIBLE,
    TAG_KEEP,
    TAG_KEEP_REVIEW,
    VETO_TAGS,
    Assessment,
    Availability,
    HistoryStatus,
    Origin,
    Outcome,
    Provenance,
    Show,
    Viewing,
)

# A series is tens to hundreds of GB and a whole run's worth of seasons to restore,
# so a third of the film cap already reclaims more and risks more per bad batch.
MAX_DELETIONS_PER_RUN = 10
SUBJECT_EXACT_BELOW = 25
# Long enough not to re-litigate weekly, short enough to catch a change within a season.
DEFAULT_RECONSIDER_DAYS = 180

# Each limit is the drop that means something broke, not merely changed.
ANOMALY_LIMITS = {
    "library_size_drop_pct": 10.0,
    "keep_tag_drop_pct": 2.0,
    "exclusion_drop_pct": 5.0,
    "history_rows_drop_pct": 20.0,
    "invalid_added_pct": 1.0,
}

KEEP_BAR_VOTES = 5000
KEEP_BAR_SCORE = 6.5


@dataclass
class PlanContext:
    """Everything the planner needs that is not the series itself."""

    now: datetime
    scope_start: datetime
    coverage_start: datetime | None
    history_ok: bool
    decisions: dict[int, dict] = field(default_factory=dict)
    genre_counts: dict[str, int] = field(default_factory=dict)


def _subject_bucket(count: int) -> int:
    """Bucket a subject count so small changes matter and large ones do not.

    Bucketed values start past the exact range, so a grouped 56 can never be
    mistaken for an exact 2.
    """
    if count < SUBJECT_EXACT_BELOW:
        return count
    return SUBJECT_EXACT_BELOW + count // SUBJECT_EXACT_BELOW


def fact_fingerprint(
    show: Show,
    provenance: Provenance,
    viewing: Viewing,
    subject_counts: dict[str, int] | None = None,
) -> str:
    """Hash the facts a spare decision rested on.

    A spare stands until one of these moves. Scores are bucketed so a rating
    drifting by 0.1 does not reopen a settled judgement, while a new viewer, a
    new season or a show ending does.
    """
    payload = {
        "score_bucket": (None if show.imdb_score is None else round(show.imdb_score * 2) / 2),
        "votes_bucket": None if show.imdb_votes is None else show.imdb_votes // 1000,
        "viewers": viewing.distinct_viewers,
        "origin": provenance.origin.value,
        "status": show.status,
        "seasons": show.season_count,
        "has_file": show.has_file,
        # Bucketed so ordinary library growth doesn't reopen a decision; a subject
        # collapsing from 20 shows to one still does.
        "subjects": {g: _subject_bucket(c) for g, c in sorted((subject_counts or {}).items())},
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


def meets_keep_bar(show: Show, bar_votes: int = KEEP_BAR_VOTES, bar_score: float = KEEP_BAR_SCORE) -> bool:
    """Whether a series clears the permanent allow-list bar on its ratings alone."""
    if show.imdb_votes is None or show.imdb_score is None:
        return False
    return show.imdb_votes >= bar_votes and show.imdb_score >= bar_score


def _protection_gate(show: Show, provenance: Provenance, result: Assessment) -> bool:
    """Apply the absolute protections. True when one of them fired."""
    vetoes = sorted(show.tags & VETO_TAGS)
    if vetoes:
        result.outcome = Outcome.PROTECTED
        result.protections.append(f"carries {', '.join(vetoes)}")
        return True

    # Protects on qualifying, not on the tag: assessment runs before tag maintenance,
    # so this stops a series being deleted in the run that would have protected it.
    if meets_keep_bar(show) and TAG_HUMAN_ELIGIBLE not in show.tags:
        result.outcome = Outcome.PROTECTED
        result.protections.append(f"meets the allow-list bar ({show.imdb_votes} votes at {show.imdb_score})")
        return True

    if provenance.origin is Origin.REQUESTED:
        result.outcome = Outcome.PROTECTED
        result.protections.append(f"explicitly requested by {provenance.requested_by or 'a household member'}")
        return True
    return False


def _scope_gate(
    show: Show,
    ctx: PlanContext,
    result: Assessment,
    available_for: int | None,
    window: int,
) -> bool:
    """Apply scope, file presence and the viewing window. True when one fired."""
    if show.status == "upcoming":
        result.outcome = Outcome.OUT_OF_SCOPE
        result.reasons.append("status is upcoming; nothing has aired")
        return True

    if show.added is None:
        result.outcome = Outcome.REVIEW
        result.blockers.append("record has no usable added date")
        return True

    if show.added < ctx.scope_start:
        result.outcome = Outcome.OUT_OF_SCOPE
        result.reasons.append(f"added {show.added.date()}, before the {ctx.scope_start.date()} scope boundary")
        return True

    if not show.has_file:
        result.outcome = Outcome.MISSING_FILE
        result.reasons.append("no episode on disk; nothing to reclaim and nobody could have watched it")
        return True

    if available_for is None:
        result.outcome = Outcome.REVIEW
        result.blockers.append("cannot establish when the series became playable")
        return True

    if available_for < window:
        result.outcome = Outcome.NOT_DUE
        result.reasons.append(f"playable for {available_for} of {window} days")
        return True
    return False


def _evidence_gate(viewing: Viewing, result: Assessment) -> bool:
    """Refuse to act on play evidence that cannot bear the weight."""
    if viewing.status is HistoryStatus.UNAVAILABLE:
        result.outcome = Outcome.REVIEW
        result.blockers.append("play history unavailable; a zero here would not mean unwatched")
        return True

    if viewing.status in (
        HistoryStatus.INCOMPLETE,
        HistoryStatus.UNRESOLVED,
        HistoryStatus.AMBIGUOUS,
    ):
        result.outcome = Outcome.REVIEW
        result.blockers.append(f"play history {viewing.status.value}: " + "; ".join(viewing.notes))
        return True

    if viewing.distinct_viewers >= 2:
        result.outcome = Outcome.REVIEW
        result.reasons.append(
            f"{viewing.distinct_viewers} distinct users finished episodes; belongs in the human queue"
        )
        return True
    return False


def assess(
    show: Show,
    provenance: Provenance,
    availability: Availability,
    viewing: Viewing,
    ctx: PlanContext,
) -> Assessment:
    """Decide what may be done with one series, before any question of taste.

    Order matters: protections come before eligibility, so a series that is both
    protected and past its window reports as protected.
    """
    window = window_days(show)
    available_for = days_available(availability, ctx.now)
    result = Assessment(
        show=show,
        outcome=Outcome.REVIEW,
        provenance=provenance,
        availability=availability,
        viewing=viewing,
        window_days=window,
        days_available=available_for,
        subject_counts={g: ctx.genre_counts.get(g, 0) for g in show.genres},
    )

    if _protection_gate(show, provenance, result):
        return result
    if _scope_gate(show, ctx, result, available_for, window):
        return result
    if _evidence_gate(viewing, result):
        return result

    # Feed provenance or a human tag authorises deletion; the tag doesn't relabel the origin.
    authorised_by = None
    if provenance.origin is Origin.FEED:
        authorised_by = "feed provenance"
    elif TAG_HUMAN_ELIGIBLE in show.tags:
        authorised_by = f"human decision tag {TAG_HUMAN_ELIGIBLE}"

    if not authorised_by:
        result.outcome = Outcome.REVIEW
        result.blockers.append(
            "origin unknown: no request record and no feed tag, so "
            "delete-by-default is not authorised for this series"
        )
        return result

    fingerprint = fact_fingerprint(show, provenance, viewing, result.subject_counts)
    stored = ctx.decisions.get(show.series_id)
    if stored and decision_still_binding(stored, fingerprint, ctx.now):
        result.outcome = Outcome.REVIEW
        verdict = stored.get("verdict", "spared")
        result.reasons.append(
            f"already {verdict} on {stored.get('decided_at', '?')[:10]}: "
            f"{stored.get('reason', 'no reason recorded')}"
        )
        return result

    if TAG_HUMAN_DISMISSED in show.tags:
        # Applied via the UI with no ledger record, so requiring one would make the tag inert.
        if not (stored and stored.get("fact_fingerprint") != fingerprint):
            result.outcome = Outcome.REVIEW
            result.reasons.append("dismissed by a person; no relevant fact has changed since")
            return result

    result.outcome = Outcome.CANDIDATE
    result.reasons.append(f"eligible: authorised by {authorised_by}, past its {window}-day window")
    return result


def keep_tag_targets(
    shows: Iterable[Show],
    bar_votes: int = KEEP_BAR_VOTES,
    bar_score: float = KEEP_BAR_SCORE,
) -> list[Show]:
    """Series that should gain the ``keep`` tag this run.

    Add-only, with two exclusions that stop maintenance overturning a person: a
    ``keep-review`` item is never promoted, because that queue exists so a human
    decides; and a series marked ``cleanup-eligible`` is never re-protected, or
    the rating rule reverses the override every week.
    """
    return [s for s in shows if keep_tag_applies(s) and meets_keep_bar(s, bar_votes, bar_score)]


def keep_tag_applies(show: Show) -> bool:
    """Whether automatic protection may still be given to this series.

    Checked again at execution because the plan is half an hour old by then: a
    person who marks a series `cleanup-eligible` in that window would otherwise
    have their decision reversed by an absolute veto tag.
    """
    if TAG_KEEP in show.tags or TAG_KEEP_REVIEW in show.tags:
        return False
    return not (TAG_HUMAN_ELIGIBLE in show.tags or TAG_HUMAN_DISMISSED in show.tags)


def select_batch(
    approved: list[Assessment],
    cap: int = MAX_DELETIONS_PER_RUN,
    already_deleted_this_window: int = 0,
) -> tuple[list[Assessment], list[Assessment]]:
    """Take the largest series first, up to what remains of the run's budget.

    There is no "too many candidates, abort everything" rule. It counts spares as
    deletions and can never drain a backlog larger than its own threshold, so it
    stalls permanently. Anomaly detection stops a run; volume does not.
    """
    remaining = max(cap - already_deleted_this_window, 0)
    ordered = sorted(approved, key=lambda a: a.show.size_bytes, reverse=True)
    return ordered[:remaining], ordered[remaining:]


def _drop_pct(current: float, baseline: float) -> float:
    """Percentage fall from baseline to current; 0 when baseline is empty."""
    if baseline <= 0:
        return 0.0
    return max(0.0, (baseline - current) / baseline * 100.0)


def snapshot_valid(snapshot: dict[str, Any]) -> list[str]:
    """Reject a snapshot too malformed to be a baseline.

    A baseline from a half-fetched run becomes the yardstick for every later run,
    so a bad one disables anomaly detection silently and permanently. Refusing to
    record is the safer failure.
    """
    problems = []
    for key in ("library_size", "keep_tag_count", "exclusion_count"):
        value = snapshot.get(key)
        if not isinstance(value, int) or value < 0:
            problems.append(f"{key} missing or not a non-negative integer")
    if isinstance(snapshot.get("library_size"), int) and snapshot["library_size"] == 0:
        problems.append("library_size is zero, which is never a valid baseline here")
    if not snapshot.get("history_ok", False):
        problems.append("history service was not healthy during this snapshot")
    rows = snapshot.get("history_rows")
    if isinstance(snapshot.get("library_size"), int) and snapshot["library_size"] > 0:
        if not isinstance(rows, int) or rows <= 0:
            # Self-ratifying if recorded: the next run compares zero to zero and
            # reads every series as a verified zero.
            problems.append("play history is empty, which cannot be a baseline for a stocked library")
    return problems


def check_anomalies(snapshot: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    """Compare this run against the last validated baseline.

    Returns the reasons deletion must not proceed; empty means the run looks
    ordinary.
    """
    if baseline is None:
        return ["no validated baseline yet; recording one and deleting nothing this run"]

    blocks: list[str] = []
    checks = (
        ("library_size", "library_size_drop_pct", "library size"),
        ("keep_tag_count", "keep_tag_drop_pct", "keep-tagged series"),
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
                f"{label} fell {drop:.1f}% ({previous} -> {current}), over the " f"{ANOMALY_LIMITS[limit_key]}% limit"
            )

    invalid = snapshot.get("invalid_added_count", 0)
    size = snapshot.get("library_size", 0) or 1
    if invalid / size * 100.0 > ANOMALY_LIMITS["invalid_added_pct"]:
        blocks.append(f"{invalid} series have an unusable added date")

    return blocks


def build_decision_record(
    assessment: Assessment,
    verdict: str,
    reason: str,
    run_id: str,
    now: datetime,
    reconsider_days: int = DEFAULT_RECONSIDER_DAYS,
) -> dict[str, Any]:
    """A spare or dismissal, with the facts it rested on and when to revisit."""
    return {
        "series_id": assessment.show.series_id,
        "tvdb_id": assessment.show.tvdb_id,
        "title": assessment.show.title,
        "verdict": verdict,
        "reason": reason,
        "decided_at": now.isoformat(),
        "run_id": run_id,
        "reconsider_after": (now + timedelta(days=reconsider_days)).isoformat(),
        "fact_fingerprint": fact_fingerprint(
            assessment.show,
            assessment.provenance,
            assessment.viewing,
            assessment.subject_counts,
        ),
    }


def summarise(assessments: Iterable[Assessment]) -> dict[str, int]:
    """Count assessments by outcome, for the report and the shape check."""
    counts: dict[str, int] = {}
    for item in assessments:
        counts[item.outcome.value] = counts.get(item.outcome.value, 0) + 1
    return counts
