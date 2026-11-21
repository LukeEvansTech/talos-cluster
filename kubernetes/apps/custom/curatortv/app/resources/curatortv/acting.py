"""The bookkeeping a run does around its deletions: tags, proposals and decisions.

Apart from the command-line module so both stay readable. Nothing here deletes;
``executor`` does that, after re-checking each title against live state.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from . import proposals
from .clients import Sonarr
from .ledger import Ledger
from .model import (
    TAG_HUMAN_ELIGIBLE,
    TAG_KEEP,
    Assessment,
    Availability,
    EpisodeProgress,
    HistoryStatus,
    Origin,
    Outcome,
    Provenance,
    Show,
    Viewing,
    utc,
)
from .planner import (
    VERDICT_REVIEW,
    VERDICT_SPARED,
    accepted_baseline,
    build_decision_record,
    keep_tag_applies,
)

# A run id names a directory in the ledger, so one taken from a setting is checked first.
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


def to_show(record: dict, tag_labels: dict[int, str]) -> Show:
    """Project a Sonarr series record onto the fields decisions depend on."""
    ratings = record.get("ratings") or {}
    stats = record.get("statistics") or {}
    votes = ratings.get("votes")
    value = ratings.get("value")
    files = int(stats.get("episodeFileCount") or 0)
    return Show(
        series_id=record["id"],
        tvdb_id=record.get("tvdbId") or None,
        tmdb_id=record.get("tmdbId") or None,
        imdb_id=record.get("imdbId") or None,
        title=record.get("title", ""),
        year=record.get("year") or None,
        status=record.get("status", ""),
        has_file=files > 0,
        size_bytes=int(stats.get("sizeOnDisk") or 0),
        added=utc(record.get("added")),
        tags=frozenset(tag_labels.get(t, str(t)) for t in record.get("tags", [])),
        imdb_score=float(value) if isinstance(value, (int, float)) and value else None,
        imdb_votes=int(votes) if isinstance(votes, int) and votes else None,
        genres=tuple(record.get("genres") or ()),
        network=record.get("network"),
        original_language=(record.get("originalLanguage") or {}).get("name"),
        overview=record.get("overview") or "",
        episode_file_count=files,
        season_count=int(stats.get("seasonCount") or 0),
    )


def rehydrate(planned: dict, show: Show, reason: str = "") -> Assessment:
    """Rebuild an assessment from a plan entry plus the live series record.

    Only the evidence needed for execution is restored; the protections are
    re-derived from live state rather than trusted from the plan.
    """
    return Assessment(
        show=show,
        outcome=Outcome.CANDIDATE,
        subject_counts=dict(planned.get("subject_counts") or {}),
        reopened_dismissal=planned.get("reopened_dismissal") is True,
        provenance=Provenance(
            Origin(planned["origin"]), tuple(planned.get("origin_evidence") or ()), planned.get("requested_by")
        ),
        availability=Availability(None, planned["availability_source"], show.has_file),
        viewing=Viewing(
            status=HistoryStatus(planned["history_status"]),
            identity_via=planned.get("identity_via", "unresolved"),
            rating_keys=tuple(planned.get("rating_keys") or ()),
            # Carried through: the fingerprint counts viewers, and dropping them would
            # record a spare against viewers=0, stale the day it's made.
            progress=tuple(
                EpisodeProgress(user_id=int(v["user_id"]), episodes=int(v["episodes"]))
                for v in (planned.get("viewers") or [])
            ),
            play_count=int(planned.get("play_count") or 0),
        ),
        reasons=[reason] if reason else [],
    )


def apply_keep_tags(  # pylint: disable=too-many-arguments
    sonarr: Sonarr,
    additions: list[dict],
    dry_run: bool,
    live: dict[int, dict] | None = None,
    tag_labels: dict[int, str] | None = None,
    tag_in_dry_run: bool = False,
) -> dict[str, Any]:
    """Give the permanent ``keep`` tag to series that have earned it.

    Add-only. The editor endpoint answers 202, meaning queued and nothing more,
    so the count is confirmed by re-reading rather than assumed.

    This may write in a dry run, when ``tag_in_dry_run`` is set: Reclaimerr
    protects anything tagged ``keep``, and an add-only tag is reversible, whereas
    a dry run that writes nothing leaves Reclaimerr blind.

    The plan is half an hour old by the time this runs, so each target is
    re-checked against its live tags first: applying ``keep`` to a series somebody
    has just marked ``cleanup-eligible`` would reverse that person's decision.
    """
    series_ids = [a["series_id"] for a in additions if isinstance(a.get("series_id"), int)]
    withdrawn = 0
    if live is not None:
        kept = []
        for series_id in series_ids:
            record = live.get(series_id)
            if record is None or not keep_tag_applies(to_show(record, tag_labels or {})):
                withdrawn += 1
                continue
            kept.append(series_id)
        series_ids = kept
    if not series_ids:
        return {"requested": 0, "applied": 0, "withdrawn": withdrawn, "note": "nothing qualified"}
    if dry_run and not tag_in_dry_run:
        return {
            "requested": len(series_ids),
            "applied": 0,
            "withdrawn": withdrawn,
            "note": "dry run: not applied",
        }

    tags = {t["label"]: t["id"] for t in sonarr.tags()}
    if TAG_KEEP not in tags:
        sonarr.create_tag(TAG_KEEP)
        tags = {t["label"]: t["id"] for t in sonarr.tags()}
    tag_id = tags[TAG_KEEP]

    status, _ = sonarr.add_tag(series_ids, tag_id)
    # Confirmed per series by re-reading, whatever the status said: a keep counted
    # as applied when it never landed is recorded as settled and never retried.
    wanted = set(series_ids)
    confirmed = sorted(s["id"] for s in sonarr.all_series() if s["id"] in wanted and tag_id in s.get("tags", []))
    unconfirmed = sorted(wanted - set(confirmed))
    note = "applied in a dry run (TAG_KEEPS_IN_DRY_RUN)" if dry_run else "applied"
    if unconfirmed:
        note = f"HTTP {status}; {len(unconfirmed)} not confirmed on re-read"
    return {
        "requested": len(series_ids),
        "applied": len(confirmed),
        "confirmed": confirmed,
        "withdrawn": withdrawn,
        "http": status,
        "note": note,
        "unconfirmed": unconfirmed,
    }


def adopt_baseline(ledger: Ledger, run_id: str, baseline: dict | None) -> tuple[dict | None, str]:
    """Honour ``CLEANUP_ACCEPT_BASELINE``: take a named earlier run's snapshot as the baseline.

    Returns the baseline to compare this run against, and a line for the log.
    """
    if not RUN_ID.fullmatch(run_id):
        return baseline, f"CLEANUP_ACCEPT_BASELINE ignored: {run_id!r} is not a run id"
    snapshot, note = accepted_baseline(ledger.read_plan(run_id), baseline)
    if snapshot is None:
        return baseline, f"CLEANUP_ACCEPT_BASELINE {run_id} ignored: {note}"
    ledger.write_baseline({**snapshot, "accepted_from_run": run_id})
    return ledger.read_baseline(), f"CLEANUP_ACCEPT_BASELINE {run_id}: {note}"


def sync_proposals(plan: dict, standing: dict[int, dict], ledger: Ledger, run_id: str) -> dict[str, int]:
    """Bring the record in line with the plan: drop what lapsed, record what a person marked.

    ``standing`` is updated in place, so what follows sees the record as it now is.
    """
    lapsed = 0
    for item in plan.get("lapsed_proposals") or []:
        series_id = item.get("series_id")
        if series_id in standing:
            ledger.delete_proposal(series_id)
            del standing[series_id]
            lapsed += 1
    marked = 0
    for entry in plan.get("proposed") or []:
        stored = standing.get(entry["series_id"]) or {}
        if (entry.get("proposal") or {}).get("by") != proposals.BY_PERSON or stored.get("by") == proposals.BY_PERSON:
            continue
        record = proposals.record(entry, "series_id", run_id)
        ledger.write_proposal(record)
        standing[entry["series_id"]] = record
        marked += 1
    return {"lapsed": lapsed, "marked": marked}


def record_verdicts(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    accepted: list[dict],
    series: dict[int, dict],
    by_id: dict[int, dict],
    tag_labels: dict[int, str],
    ledger: Ledger,
    run_id: str,
    grace_days: int,
) -> tuple[list[dict], dict[str, int]]:
    """Put this run's verdicts on record, and collect what the judge sent to a person.

    A delete or a keep becomes a proposal that waits ``grace_days``; nothing is
    done about either in the run that made it. A review is recorded as before
    and returned, because the plan's review list was written before judgement:
    without carrying it out of here, a series the judge put in front of somebody
    would wait until next week's plan to be seen.
    """
    now = datetime.now(timezone.utc)
    escalated: list[dict] = []
    recorded = {proposals.DELETE: 0, proposals.KEEP: 0}
    for item in accepted:
        series_id = item["series_id"]
        if series_id not in series:
            continue
        if item["verdict"] in proposals.VERDICTS:
            proposal = proposals.judged(item["verdict"], item["reason"], now, grace_days)
            ledger.write_proposal(proposals.record({**by_id[series_id], "proposal": proposal}, "series_id", run_id))
            recorded[item["verdict"]] += 1
            continue
        show = to_show(series[series_id], tag_labels)
        escalated.append(
            {
                "series_id": show.series_id,
                "title": show.title,
                "year": show.year,
                "size_gb": show.size_gb,
                "reason": item["reason"],
            }
        )
        ledger.write_decision(
            build_decision_record(
                rehydrate(by_id[series_id], show),
                verdict=VERDICT_REVIEW,
                reason=item["reason"],
                run_id=run_id,
                now=now,
            )
        )
    return escalated, recorded


def _closes_untagged(item) -> bool:
    """A due keep that will never be tagged, and should close as a spare instead.

    ``cleanup-dismissed``, ``keep-review`` or an existing ``keep`` stops the tag for
    good. ``cleanup-eligible`` does too, but that title's proposal is replaced by
    the person's delete at the next plan, so it is left for that.
    """
    return not keep_tag_applies(item) and TAG_HUMAN_ELIGIBLE not in item.tags


def _settled(tagged: dict[str, Any], due: dict[int, dict], live: dict[int, dict], closes) -> list[int]:
    """The due keeps whose proposal closes this run: tagged and confirmed, or never to be tagged.

    Waiting for a tag that is never applied would hold a proposal open for good,
    so such a keep closes as a recorded spare, which lapses after the usual 180
    days or when a fact changes. ``tagged`` gains the count.
    """
    suppressed = sorted(ident for ident in due if ident in live and closes(live[ident]))
    tagged["settled_untagged"] = len(suppressed)
    return sorted(set(tagged.get("confirmed") or []) | set(suppressed))


def settle_keeps(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sonarr: Sonarr,
    entries: list[dict],
    standing: dict[int, dict],
    series: dict[int, dict],
    tag_labels: dict[int, str],
    ledger: Ledger,
    run_id: str,
    tag_in_dry_run: bool = False,
) -> dict[str, Any]:
    """Give the due keeps their tag, and close each proposal whose tag is confirmed.

    A keep the judge handed down joins the allow list only now, a review window
    after the verdict. One whose tag did not land stays a proposal, so the next
    run tries again rather than treating it as settled.
    """
    due = {entry["series_id"]: entry for entry in entries if proposals.backed(entry, standing.get(entry["series_id"]))}
    tagged = apply_keep_tags(
        sonarr,
        [{"series_id": series_id} for series_id in due],
        ledger.dry_run,
        live=series,
        tag_labels=tag_labels,
        tag_in_dry_run=tag_in_dry_run,
    )
    now = datetime.now(timezone.utc)
    for series_id in _settled(tagged, due, series, lambda record: _closes_untagged(to_show(record, tag_labels))):
        show = to_show(series[series_id], tag_labels)
        ledger.write_decision(
            build_decision_record(
                rehydrate(due[series_id], show),
                verdict=VERDICT_SPARED,
                reason=str(due[series_id]["proposal"].get("reason") or ""),
                run_id=run_id,
                now=now,
            )
        )
        ledger.delete_proposal(series_id)
    return tagged


def due_deletes(
    entries: list[dict],
    standing: dict[int, dict],
    series: dict[int, dict],
    tag_labels: dict[int, str],
) -> tuple[list[Assessment], list[dict], frozenset[int]]:
    """Rebuild the due deletes that are still what the plan says they are.

    Returns the ones to act on, the ones set aside with why, and the ids a person
    marked. A judge's delete has to be on record; a person's rests on the tag, so
    taking the tag off since the plan withdraws it.
    """
    approved: list[Assessment] = []
    set_aside: list[dict] = []
    marked: set[int] = set()
    for entry in entries:
        series_id, proposal = entry["series_id"], entry.get("proposal") or {}
        label = {key: entry.get(key) for key in ("series_id", "title", "year", "size_gb")}
        record = series.get(series_id)
        if record is None:
            set_aside.append({**label, "why": "series is no longer in Sonarr"})
            continue
        show = to_show(record, tag_labels)
        if proposal.get("by") == proposals.BY_PERSON:
            if TAG_HUMAN_ELIGIBLE not in show.tags:
                set_aside.append({**label, "why": "cleanup-eligible was removed since the plan"})
                continue
            marked.add(series_id)
        elif not proposals.backed(entry, standing.get(series_id)):
            set_aside.append({**label, "why": "no matching proposal is on record; not acted on"})
            continue
        assessment = rehydrate(entry, show, str(proposal.get("reason") or ""))
        assessment.marked_by_person = proposal.get("by") == proposals.BY_PERSON
        approved.append(assessment)
    return approved, set_aside, frozenset(marked)
