"""Verdicts that wait before anything is done about them.

A judge's ``delete`` or ``keep`` is a proposal: it is recorded with the facts it
rested on and the date it falls due, sits where a person can overrule it, and is
acted on by the first run that finds it due. A person's ``cleanup-eligible`` tag
is a proposal too, due at once.

Pure functions over plain dicts, so both engines share this file unchanged; the
only thing that differs between them is the name of the id field.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# One day short of the weekly schedule: a verdict recorded minutes into one run has to
# be due already when the next run starts at the same time a week later.
DEFAULT_GRACE_DAYS = 6
# How far apart runs are, for telling a person which run a proposal goes at.
RUN_CADENCE_DAYS = 7

DELETE = "delete"
KEEP = "keep"
VERDICTS = (DELETE, KEEP)
BY_JUDGE = "judge"
BY_PERSON = "person"
PERSON_REASON = "Marked for deletion by a person (cleanup-eligible)."


def when(value: Any) -> datetime | None:
    """Parse a stored timestamp into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def judged(verdict: str, reason: str, now: datetime, grace_days: int) -> dict[str, Any]:
    """The proposal a judge's verdict becomes: nothing happens until ``due_at``."""
    return {
        "verdict": verdict,
        "by": BY_JUDGE,
        "reason": reason,
        "proposed_at": now.isoformat(),
        "due_at": (now + timedelta(days=grace_days)).isoformat(),
    }


def by_person(standing: dict | None, now: datetime) -> dict[str, Any]:
    """The proposal a ``cleanup-eligible`` tag stands for: a delete, due at once.

    One already on record keeps its dates, so a title held back by the cap is
    not shown as newly marked every week.
    """
    if standing and standing.get("by") == BY_PERSON and standing.get("verdict") == DELETE:
        return summary(standing)
    stamp = now.isoformat()
    return {"verdict": DELETE, "by": BY_PERSON, "reason": PERSON_REASON, "proposed_at": stamp, "due_at": stamp}


def summary(stored: dict) -> dict[str, Any]:
    """The fields of a stored proposal that a plan carries on its title."""
    return {key: stored.get(key) for key in ("verdict", "by", "reason", "proposed_at", "due_at")}


def stands(stored: dict, fingerprint: str | None, play_count: int) -> bool:
    """Whether a judge's stored proposal still rests on today's facts.

    The fingerprint covers what a spare rests on; the play count is checked as
    well because a proposal is a deletion in waiting, and anybody starting the
    title since the verdict is reason enough to ask again.
    """
    if stored.get("by") != BY_JUDGE or stored.get("verdict") not in VERDICTS:
        return False
    if when(stored.get("due_at")) is None or not fingerprint:
        return False
    if stored.get("fact_fingerprint") != fingerprint:
        return False
    return stored.get("play_count") == play_count


def is_due(proposal: dict, now: datetime) -> bool:
    """Whether a proposal's wait is over. One with no usable date never is."""
    due = when(proposal.get("due_at"))
    return due is not None and due <= now


def record(entry: dict, id_key: str, run_id: str) -> dict[str, Any]:
    """The ledger record for a plan entry carrying a proposal.

    The entry is stored whole: it is the evidence the verdict was given on, and
    what the review document shows a person.
    """
    proposal = entry.get("proposal") or {}
    facts = {key: value for key, value in entry.items() if key != "proposal"}
    return {
        id_key: entry[id_key],
        "title": entry.get("title"),
        "year": entry.get("year"),
        **summary(proposal),
        "run_id": run_id,
        "fact_fingerprint": entry.get("fact_fingerprint"),
        "play_count": int(entry.get("play_count") or 0),
        "entry": facts,
    }


def backed(entry: dict, stored: dict | None) -> bool:
    """Whether the ledger holds the judge's proposal a plan entry claims.

    A plan is a file on a volume; a due delete has to be on record as well, with
    the same verdict and date, before it is acted on.
    """
    proposal = entry.get("proposal") or {}
    if not stored or stored.get("by") != BY_JUDGE:
        return False
    return all(stored.get(key) == proposal.get(key) for key in ("verdict", "by", "proposed_at", "due_at"))


def split_due(entries: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    """The due deletes and the due keeps among a plan's proposed titles."""
    due = [entry for entry in entries if is_due(entry.get("proposal") or {}, now)]
    deletes = [entry for entry in due if entry["proposal"].get("verdict") == DELETE]
    keeps = [entry for entry in due if entry["proposal"].get("verdict") == KEEP]
    return deletes, keeps


def next_run(planned_at: datetime | None, now: datetime) -> datetime:
    """When the run after this one starts, taking the schedule from this run's plan."""
    return (planned_at or now) + timedelta(days=RUN_CADENCE_DAYS)
