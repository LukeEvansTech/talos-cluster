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

from . import proposals
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
    Outcome,
    Provenance,
    Viewing,
)

MAX_DELETIONS_PER_RUN = 30
# A day short of the weekly schedule. Last week's deletions were recorded minutes after the
# moment this week's run counts from, so a seven-day window would still hold every one of
# them and each second run would delete nothing.
CAP_WINDOW_DAYS = 6
# Bounds one run's judging cost and time; the rest of a backlog waits its turn.
DEFAULT_JUDGE_LIMIT = 150
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

# What the judge's two non-delete verdicts are recorded as. A spare is settled; a
# review is still waiting on a person, and the planner tells them apart by this.
VERDICT_SPARED = "spared"
VERDICT_REVIEW = "queued for review"


@dataclass
class PlanContext:
    """Everything the planner needs that is not the film itself."""

    now: datetime
    # None means the whole library is in scope.
    scope_start: datetime | None
    monitored_collection_tmdb_ids: frozenset[int]
    coverage_start: datetime | None
    history_ok: bool
    decisions: dict[int, dict] = field(default_factory=dict)
    # Verdicts on record and waiting out their review window, keyed by id.
    proposals: dict[int, dict] = field(default_factory=dict)
    collections_loaded: bool = True
    # Computed once over the library; assessment needs it to build the fingerprint.
    siblings_owned: dict[int, int] = field(default_factory=dict)
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
    film: Film,
    provenance: Provenance,
    viewing: Viewing,
    monitored: bool,
    siblings_owned: int = 0,
    subject_counts: dict[str, int] | None = None,
) -> str:
    """Hash the facts a spare decision rested on.

    A spare stands until one of these moves. Scores are bucketed so a rating
    drifting by 0.1 does not reopen a settled judgement, while a new completion
    or a newly monitored collection does.
    """
    payload = {
        "score_bucket": (None if film.imdb_score is None else round(film.imdb_score * 2) / 2),
        "votes_bucket": None if film.imdb_votes is None else film.imdb_votes // 1000,
        "completers": viewing.distinct_completers,
        "origin": provenance.origin.value,
        "monitored_collection": monitored,
        "collection": film.collection_title,
        "has_file": film.has_file,
        # A spare must not survive its sibling being removed from the collection.
        "siblings_owned": siblings_owned,
        # Bucketed so ordinary library growth doesn't reopen a decision; a subject
        # collapsing from 20 films to one still does.
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


def in_monitored_collection(film: Film, ctx: PlanContext) -> bool:
    """Membership of a collection Radarr has been told to complete."""
    return bool(film.tmdb_id and film.tmdb_id in ctx.monitored_collection_tmdb_ids)


def _protection_gate(
    film: Film,
    ctx: PlanContext,
    result: Assessment,
    monitored: bool,
) -> bool:
    """Apply the protections that need no play evidence. True when one fired.

    A request is deliberately absent: a requested film waits out its window like
    any other and reaches the judge with its origin on the record.
    """
    vetoes = sorted(film.tags & VETO_TAGS)
    if vetoes:
        result.outcome = Outcome.PROTECTED
        result.protections.append(f"carries {', '.join(vetoes)}")
        return True

    # Protects on qualifying, not on the tag: assessment runs before tag maintenance,
    # so this stops a film being deleted in the run that would have protected it.
    if meets_keep_bar(film) and TAG_HUMAN_ELIGIBLE not in film.tags:
        result.outcome = Outcome.PROTECTED
        result.protections.append(f"meets the allow-list bar ({film.imdb_votes} votes at {film.imdb_score})")
        return True

    if monitored:
        result.outcome = Outcome.PROTECTED
        result.protections.append("belongs to a monitored collection; completing that set is a standing instruction")
        return True

    if not ctx.collections_loaded:
        result.outcome = Outcome.REVIEW
        result.blockers.append("collection data unavailable, so collection protection cannot be checked")
        return True
    return False


def _scope_gate(
    film: Film,
    ctx: PlanContext,
    result: Assessment,
    available_for: int | None,
    window: int,
) -> bool:
    """Apply scope, file presence and the viewing window. True when one fired."""
    if film.status != "released":
        result.outcome = Outcome.OUT_OF_SCOPE
        result.reasons.append(f"status is {film.status}, not released")
        return True

    if film.added is None:
        result.outcome = Outcome.REVIEW
        result.blockers.append("record has no usable added date")
        return True

    if ctx.scope_start and film.added < ctx.scope_start:
        result.outcome = Outcome.OUT_OF_SCOPE
        result.reasons.append(f"added {film.added.date()}, before the {ctx.scope_start.date()} scope boundary")
        return True

    if not film.has_file:
        result.outcome = Outcome.MISSING_FILE
        result.reasons.append("no file on disk; nothing to reclaim and nobody could have watched it")
        return True

    if available_for is None:
        result.outcome = Outcome.REVIEW
        result.blockers.append("cannot establish when the film became playable")
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

    # Last of the protections, and here rather than with the others because it is the
    # only one that rests on play history: an untrustworthy count goes to a person above.
    if viewing.distinct_completers >= 2:
        result.outcome = Outcome.PROTECTED
        result.protections.append(f"watched by the house: {viewing.distinct_completers} distinct users finished it")
        return True
    return False


def _settled(film: Film, viewing: Viewing, ctx: PlanContext, result: Assessment, fingerprint: str) -> bool:
    """Apply what a person or an earlier run already decided about a film. True when something did.

    Reached only once every gate has passed, so nothing here can outrank a protection.
    """
    stored = ctx.decisions.get(film.movie_id)
    # Before the stored decision: dismissing is how a person answers a review the judge
    # asked for, and that review would otherwise keep the film listed as waiting.
    if TAG_HUMAN_DISMISSED in film.tags:
        # Applied via the UI with no ledger record, so requiring one would make the tag inert.
        if not (stored and stored.get("fact_fingerprint") != fingerprint):
            result.outcome = Outcome.SPARED
            result.reasons.append("dismissed by a person; no relevant fact has changed since")
            return True

    standing = ctx.proposals.get(film.movie_id)
    # A person's delete takes the judge's place: it is never sent for a verdict, and no
    # stored spare, review or standing keep outranks it. The gates above still do, and so
    # does a dismissal beside it, even one a changed fact has reopened for the judge.
    if TAG_HUMAN_ELIGIBLE in film.tags and TAG_HUMAN_DISMISSED not in film.tags:
        result.outcome = Outcome.PROPOSED
        result.proposal = proposals.by_person(standing, ctx.now)
        result.reasons.append("marked cleanup-eligible by a person; due at this run")
        return True

    if stored and decision_still_binding(stored, fingerprint, ctx.now):
        verdict = stored.get("verdict", VERDICT_SPARED)
        # The judge's own review stays in front of a person until they act on it;
        # a spare is settled and asks nothing of anyone.
        result.outcome = Outcome.REVIEW if verdict == VERDICT_REVIEW else Outcome.SPARED
        # A judge's keep is a spare with an end date; it says so, so nobody reads it as for good.
        kept_until = stored.get("kept_until")
        settled = f"already {verdict} on {stored.get('decided_at', '?')[:10]}"
        if kept_until:
            settled = f"kept by the judge on {stored.get('decided_at', '?')[:10]} until {str(kept_until)[:10]}"
        result.reasons.append(f"{settled}: {stored.get('reason', 'no reason recorded')}")
        return True

    # A verdict already on record is not asked for again while its facts hold; one whose
    # facts moved falls through and is judged afresh.
    if standing and proposals.stands(standing, fingerprint, viewing.play_count):
        result.outcome = Outcome.PROPOSED
        result.proposal = proposals.summary(standing)
        result.reasons.append(
            f"{standing.get('verdict')} proposed on {str(standing.get('proposed_at'))[:10]}, "
            f"due {str(standing.get('due_at'))[:10]}: {standing.get('reason', 'no reason recorded')}"
        )
        return True
    return False


def assess(
    film: Film,
    provenance: Provenance,
    availability: Availability,
    viewing: Viewing,
    ctx: PlanContext,
) -> Assessment:
    """Decide what may be done with one film, before any question of taste.

    Order matters: the allow list, the rating bar and a monitored collection
    come before eligibility, so a film that is both protected and past its window
    reports as protected. Two or more completers protect too, but only once the
    play history has been shown to be trustworthy.
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
        siblings_owned=ctx.siblings_owned.get(film.movie_id, 0),
        subject_counts={g: ctx.genre_counts.get(g, 0) for g in film.genres},
    )

    if _protection_gate(film, ctx, result, monitored):
        return result
    if _scope_gate(film, ctx, result, available_for, window):
        return result
    if _evidence_gate(viewing, result):
        return result

    # No origin gate: how a film arrived is evidence for the judge, not a permission.
    # Requiring an import-list tag left all but two films of a whole library undeletable.
    fingerprint = fact_fingerprint(film, provenance, viewing, monitored, result.siblings_owned, result.subject_counts)
    result.fingerprint = fingerprint
    if _settled(film, viewing, ctx, result, fingerprint):
        return result

    result.outcome = Outcome.CANDIDATE
    result.reasons.append(f"eligible: past its {window}-day window, origin {provenance.origin.value}")
    return result


KEEP_BAR_VOTES = 5000
KEEP_BAR_SCORE = 6.5


def meets_keep_bar(film: Film, bar_votes: int = KEEP_BAR_VOTES, bar_score: float = KEEP_BAR_SCORE) -> bool:
    """Whether a film clears the permanent allow-list bar on its ratings alone."""
    if film.imdb_votes is None or film.imdb_score is None:
        return False
    return film.imdb_votes >= bar_votes and film.imdb_score >= bar_score


def keep_tag_targets(
    films: Iterable[Film],
    bar_votes: int = KEEP_BAR_VOTES,
    bar_score: float = KEEP_BAR_SCORE,
) -> list[Film]:
    """Films that should gain the ``keep`` tag this run.

    Add-only, with two exclusions that stop maintenance overturning a person: a
    ``keep-review`` item is never promoted, because that queue exists so a human
    decides; and a film marked ``cleanup-eligible`` is never re-protected, or the
    rating rule reverses the override every week.
    """
    return [f for f in films if keep_tag_applies(f) and meets_keep_bar(f, bar_votes, bar_score)]


def keep_tag_applies(film: Film) -> bool:
    """Whether automatic protection may still be given to this film.

    Checked again at execution because the plan is half an hour old by then: a
    person who marks a film `cleanup-eligible` in that window would otherwise
    have their decision reversed by an absolute veto tag.
    """
    if TAG_KEEP in film.tags or TAG_KEEP_REVIEW in film.tags:
        return False
    return not (TAG_HUMAN_ELIGIBLE in film.tags or TAG_HUMAN_DISMISSED in film.tags)


def select_batch(
    approved: list[Assessment],
    cap: int = MAX_DELETIONS_PER_RUN,
    already_deleted_this_window: int = 0,
    first: frozenset[int] = frozenset(),
) -> tuple[list[Assessment], list[Assessment]]:
    """Take the largest files first, up to what remains of the run's budget.

    There is no "too many candidates, abort everything" rule. It counts spares as
    deletions and can never drain a backlog larger than its own threshold, so it
    stalls permanently. Anomaly detection stops a run; volume does not.
    """
    remaining = max(cap - already_deleted_this_window, 0)
    # Ids in `first` go ahead of size: a person's own delete must not queue for weeks
    # behind a backlog of larger titles the judge chose.
    ordered = sorted(approved, key=lambda a: (a.film.movie_id in first, a.film.size_bytes), reverse=True)
    return ordered[:remaining], ordered[remaining:]


def select_for_judgement(
    candidates: list[Assessment], limit: int = DEFAULT_JUDGE_LIMIT
) -> tuple[list[Assessment], list[Assessment]]:
    """Split the candidates into those the judge is asked about this run, and the rest.

    Largest first, the order deletion takes them in. The remainder is deferred,
    not unjudged: it is never offered, so no verdict is owed for it and it comes
    back next run.
    """
    return select_batch(candidates, cap=limit)


def count_owned_siblings(movies: list[dict], collections: list[dict]) -> dict[int, int]:
    """How many *other* entries of a film's collection are already in the library.

    "Collection support" is one of the reasons to keep a film, and it is
    unanswerable from a collection name alone.
    """
    # Only films on disk count. A collection entry Radarr is still looking for
    # is not evidence that the library holds part of the set.
    owned = {m.get("tmdbId") for m in movies if m.get("tmdbId") and m.get("hasFile")}
    by_movie: dict[int, int] = {}
    tmdb_to_id = {m["tmdbId"]: m["id"] for m in movies if m.get("tmdbId")}
    for collection in collections:
        members = [e.get("tmdbId") for e in (collection.get("movies") or []) if e.get("tmdbId")]
        held = [t for t in members if t in owned]
        for tmdb_id in held:
            movie_id = tmdb_to_id.get(tmdb_id)
            if movie_id is not None:
                by_movie[movie_id] = len(held) - 1
    return by_movie


def count_genres(movies: list[dict]) -> dict[str, int]:
    """How many films the library holds per genre.

    "A subject the library demonstrably follows" is meant to be counted, not
    assumed, so the count has to come from the library rather than a guess.
    """
    counts: dict[str, int] = {}
    for movie in movies:
        if not movie.get("hasFile"):
            continue
        for genre in movie.get("genres") or []:
            counts[genre] = counts.get(genre, 0) + 1
    return counts


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
    for key in (
        "library_size",
        "keep_tag_count",
        "collection_count",
        "exclusion_count",
    ):
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
            # reads every film as a verified zero.
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
                f"{label} fell {drop:.1f}% ({previous} -> {current}), over the " f"{ANOMALY_LIMITS[limit_key]}% limit"
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


def lapsed_proposals(standing: dict[int, dict], assessments: Iterable[Assessment]) -> list[dict[str, Any]]:
    """Proposals on record that this plan no longer upholds, each with why.

    Anything but a PROPOSED outcome ends one: a protection that now applies, evidence
    that can no longer be trusted, facts that moved, or a film that has left the library.
    """
    assessed = {a.film.movie_id: a for a in assessments if a.film.movie_id in standing}
    lapsed = []
    for item_id, stored in sorted(standing.items()):
        found = assessed.get(item_id)
        if found is not None and found.outcome is Outcome.PROPOSED:
            continue
        why = "no longer in the library"
        if found is not None:
            detail = (found.protections or found.blockers or found.reasons or ["no reason recorded"])[0]
            why = f"now {found.outcome.value}: {detail}"
        lapsed.append(
            {
                "movie_id": item_id,
                "title": stored.get("title"),
                "verdict": stored.get("verdict"),
                "by": stored.get("by"),
                "why": why,
            }
        )
    return lapsed


def accepted_baseline(named: dict | None, baseline: dict | None) -> tuple[dict[str, Any] | None, str]:
    """The snapshot of an earlier run that an operator has accepted as the new baseline.

    ``named`` is that run's stored plan. Its figures were reported when it was blocked,
    so accepting them is accepting something a person has seen, which accepting whatever
    the next run finds would not be. A snapshot no newer than the baseline is refused,
    which is what makes the setting safe to leave in place: it is honoured once.
    """
    snapshot = (named or {}).get("snapshot")
    if not isinstance(snapshot, dict):
        return None, "no plan with a snapshot is on record for that run in this mode"
    problems = snapshot_valid(snapshot)
    if problems:
        return None, f"that run's snapshot cannot be a baseline: {'; '.join(problems)}"
    taken = proposals.when(snapshot.get("taken_at"))
    if taken is None:
        return None, "that run's snapshot carries no usable taken_at"
    if baseline is not None:
        current = proposals.when(baseline.get("taken_at"))
        if current is None:
            return None, "the baseline on record carries no usable taken_at to compare with"
        if taken <= current:
            return None, "the baseline is already as new as that run; nothing to accept"
    return snapshot, f"accepted the snapshot taken {taken.isoformat()} as the baseline"


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
            assessment.film,
            assessment.provenance,
            assessment.viewing,
            monitored,
            assessment.siblings_owned,
            assessment.subject_counts,
        ),
    }


def summarise(assessments: Iterable[Assessment]) -> dict[str, int]:
    """Count assessments by outcome, for the report and the shape check."""
    counts: dict[str, int] = {}
    for item in assessments:
        counts[item.outcome.value] = counts.get(item.outcome.value, 0) + 1
    return counts
