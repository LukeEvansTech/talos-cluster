"""Verdicts that wait before anything is done about them.

A judge's ``delete`` or ``keep`` is a proposal: it is recorded with the facts it
rested on and the date it falls due, sits where a person can overrule it, and is
acted on by the first run that finds it due. A due keep is not a tag: it becomes a
stored decision that spares the title for ``DEFAULT_KEEP_DAYS``. A person's ``cleanup-eligible`` tag
is a proposal too, due at once.

Pure functions over plain dicts, so both engines share this file unchanged; the
only thing that differs between them is the name of the id field.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

# One day short of the weekly schedule: a verdict recorded minutes into one run has to
# be due already when the next run starts at the same time a week later.
DEFAULT_GRACE_DAYS = 6
# How far apart runs are, for telling a person which run a proposal goes at when the
# CronJob's own schedule was not passed in or cannot be read.
RUN_CADENCE_DAYS = 7
# The lowest and highest value of each field of a cron line: minute, hour, day, month, weekday.
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
# How far ahead a firing is looked for before the schedule counts as unreadable.
SCHEDULE_HORIZON_DAYS = 366
# How long a judge's keep spares a title once its wait is over (CLEANUP_KEEP_DAYS). Only a
# person grants forever; the judge's keep is judged again a year on, with that year's plays.
DEFAULT_KEEP_DAYS = 365

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


def _cron_field(text: str, low: int, high: int) -> set[int] | None:
    """The values one cron field allows, or None when it is not numbers, lists, ranges and steps."""
    allowed: set[int] = set()
    for part in text.split(","):
        span, slash, step = part.partition("/")
        if slash and (not step.isdigit() or int(step) == 0):
            return None
        first, dash, last = span.partition("-")
        if span == "*":
            start, end = low, high
        elif first.isdigit() and (last.isdigit() if dash else not last):
            start = int(first)
            # "5/15" runs from 5 to the top of the field, as cron reads it.
            end = int(last) if dash else (high if slash else start)
        else:
            return None
        if not low <= start <= end <= high:
            return None
        allowed.update(range(start, end + 1, int(step) if slash else 1))
    return allowed


def _fires_on(day: date, fields: list[set[int]], either: bool) -> bool:
    """Whether a cron line fires on this date. Cron counts Sunday as 0 or 7."""
    if day.month not in fields[3]:
        return False
    on_date, on_weekday = day.day in fields[2], bool({day.isoweekday() % 7, day.isoweekday()} & fields[4])
    return (on_date or on_weekday) if either else (on_date and on_weekday)


def _cron(schedule: str, zone: str) -> tuple[list[str], list[set[int]], tzinfo] | None:
    """A five-field cron line as its texts, the values each field allows and its zone, or None.

    Read as Kubernetes reads a CronJob's ``schedule`` and ``timeZone`` (UTC when ``zone`` is
    empty). A month or weekday name, ``?`` or an ``@weekly`` style macro is not read.
    """
    texts = schedule.split()
    if len(texts) != 5:
        return None
    fields: list[set[int]] = []
    for text, (low, high) in zip(texts, CRON_RANGES):
        allowed = _cron_field(text, low, high)
        if allowed is None:
            return None
        fields.append(allowed)
    try:
        local: tzinfo = ZoneInfo(zone) if zone else timezone.utc
    except (KeyError, ValueError, OSError):
        return None
    return texts, fields, local


def _firing(schedule: str, zone: str, at: datetime, forward: bool) -> datetime | None:
    """The first firing after ``at``, or the last one at or before it, in UTC."""
    cron = _cron(schedule, zone)
    if cron is None:
        return None
    texts, fields, local = cron
    # Cron's rule: when the day and the weekday are both restricted, either one matching is enough.
    either = all(text not in ("*", "*/1") for text in (texts[2], texts[4]))
    first = at.astimezone(local).date()
    for offset in range(SCHEDULE_HORIZON_DAYS + 1):
        day = first + timedelta(days=offset if forward else -offset)
        if not _fires_on(day, fields, either):
            continue
        for hour in sorted(fields[1], reverse=not forward):
            for minute in sorted(fields[0], reverse=not forward):
                fires = datetime(day.year, day.month, day.day, hour, minute, tzinfo=local)
                if (fires > at) if forward else (fires <= at):
                    return fires.astimezone(timezone.utc)
    return None


def next_firing(schedule: str, zone: str, after: datetime) -> datetime | None:
    """When a five-field cron line next fires after ``after``, in UTC, or None if it cannot be read."""
    return _firing(schedule, zone, after, forward=True)


def run_start(now: datetime, schedule: str = "", zone: str = "") -> datetime:
    """The scheduled start of the run that is executing at ``now``: what "due" is measured against.

    The review page lists a proposal as going at the next run only when it is due by that
    run's scheduled start. A run takes minutes to reach its execute step, so measuring against
    the clock would act on a proposal the page listed as waiting. Measured against the start,
    a run acts on exactly what the page said it would, and one started by hand between two
    firings acts on what the earlier firing was due to take.

    Without a schedule that can be read there is no start to name, and it is ``now``.
    """
    fires = _firing(schedule, zone, now, forward=False) if schedule else None
    return fires or now


def next_run(planned_at: datetime | None, now: datetime, schedule: str = "", zone: str = "") -> datetime:
    """When the run after this one starts.

    Taken from the CronJob's own ``schedule`` when the caller has it, so a run started by
    hand on another day still names the scheduled one. Without a schedule that can be read
    it is a week after this run's plan, which holds only for a run that started on time.
    """
    fires = next_firing(schedule, zone, now) if schedule else None
    return fires or (planned_at or now) + timedelta(days=RUN_CADENCE_DAYS)
