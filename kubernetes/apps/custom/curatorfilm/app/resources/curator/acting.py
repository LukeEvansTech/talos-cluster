"""The bookkeeping a run does around its deletions: tags, proposals and decisions.

Only the rating bar writes the ``keep`` tag. A judge's keep becomes a decision
that spares the film for a year (``settle_keeps``); only a person keeps for good.

Apart from the command-line module so both stay readable. Nothing here deletes;
``executor`` does that, after re-checking each title against live state.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import proposals
from .clients import Radarr
from .ledger import Ledger
from .model import (
    TAG_HUMAN_ELIGIBLE,
    TAG_KEEP,
    VETO_TAGS,
    Assessment,
    Availability,
    Completion,
    Film,
    HistoryStatus,
    Origin,
    Outcome,
    Provenance,
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


def to_film(record: dict, tag_labels: dict[int, str]) -> Film:
    """Project a Radarr movie record onto the fields decisions depend on."""
    ratings = (record.get("ratings") or {}).get("imdb") or {}
    votes = ratings.get("votes")
    value = ratings.get("value")
    return Film(
        movie_id=record["id"],
        tmdb_id=record.get("tmdbId"),
        imdb_id=record.get("imdbId"),
        title=record.get("title", ""),
        year=record.get("year"),
        status=record.get("status", ""),
        has_file=bool(record.get("hasFile")),
        size_bytes=int(record.get("sizeOnDisk") or 0),
        added=utc(record.get("added")),
        tags=frozenset(tag_labels.get(t, str(t)) for t in record.get("tags", [])),
        imdb_score=float(value) if isinstance(value, (int, float)) and value else None,
        imdb_votes=int(votes) if isinstance(votes, int) and votes else None,
        collection_title=(record.get("collection") or {}).get("title"),
        genres=tuple(record.get("genres") or ()),
        studio=record.get("studio"),
        original_language=(record.get("originalLanguage") or {}).get("name"),
        overview=record.get("overview") or "",
        file_date_added=utc((record.get("movieFile") or {}).get("dateAdded")),
    )


def rehydrate(planned: dict, film: Film, reason: str = "") -> Assessment:
    """Rebuild an assessment from a plan entry plus the live movie record.

    Only the evidence needed for execution is restored; the protections are
    re-derived from live state rather than trusted from the plan.
    """
    return Assessment(
        film=film,
        outcome=Outcome.CANDIDATE,
        # Carried so the decision fingerprint covers the evidence a keep was
        # actually granted on: losing the last owned sibling should reopen it.
        siblings_owned=int(planned.get("siblings_owned") or 0),
        subject_counts=dict(planned.get("subject_counts") or {}),
        reopened_dismissal=planned.get("reopened_dismissal") is True,
        provenance=Provenance(
            Origin(planned["origin"]), tuple(planned.get("origin_evidence") or ()), planned.get("requested_by")
        ),
        availability=Availability(None, planned["availability_source"], film.has_file),
        viewing=Viewing(
            status=HistoryStatus(planned["history_status"]),
            identity_via=planned.get("identity_via", "unresolved"),
            rating_keys=tuple(planned.get("rating_keys") or ()),
            # Carried through: the fingerprint counts completers, and dropping it would
            # record a spare against completers=0, stale the day it's made.
            completions=tuple(
                Completion(user_id=int(c["user_id"]), percent=int(c["percent"]))
                for c in (planned.get("completions") or [])
            ),
            play_count=int(planned.get("play_count") or 0),
        ),
        reasons=[reason] if reason else [],
    )


def apply_keep_tags(  # pylint: disable=too-many-arguments
    radarr: Radarr,
    additions: list[dict],
    dry_run: bool,
    live: dict[int, dict] | None = None,
    tag_labels: dict[int, str] | None = None,
    tag_in_dry_run: bool = False,
) -> dict[str, Any]:
    """Give the ``keep`` tag to films the rating bar protects.

    Add-only. The editor endpoint answers 202, meaning queued and nothing more,
    so each tag is confirmed by re-reading rather than assumed.

    A dry run writes nothing unless ``tag_in_dry_run`` is set: an add-only tag
    is reversible, and without it a rehearsal leaves every other tool that
    honours ``keep`` blind to the decisions made here.

    The plan is half an hour old by the time this runs, so each target is
    re-checked against its live tags first: ``keep`` is an absolute veto, and
    applying it to a film somebody has just marked ``cleanup-eligible`` would
    reverse that person's decision.
    """
    movie_ids = [a["movie_id"] for a in additions if isinstance(a.get("movie_id"), int)]
    withdrawn = 0
    if live is not None:
        kept = []
        for movie_id in movie_ids:
            record = live.get(movie_id)
            if record is None or not keep_tag_applies(to_film(record, tag_labels or {})):
                withdrawn += 1
                continue
            kept.append(movie_id)
        movie_ids = kept
    if not movie_ids:
        return {"requested": 0, "applied": 0, "withdrawn": withdrawn, "note": "nothing qualified"}
    if dry_run and not tag_in_dry_run:
        return {
            "requested": len(movie_ids),
            "applied": 0,
            "withdrawn": withdrawn,
            "note": "dry run: not applied",
        }

    tags = {t["label"]: t["id"] for t in radarr.tags()}
    if TAG_KEEP not in tags:
        radarr.create_tag(TAG_KEEP)
        tags = {t["label"]: t["id"] for t in radarr.tags()}
    tag_id = tags[TAG_KEEP]

    status, _ = radarr.add_tag(movie_ids, tag_id)
    # Confirmed per film by re-reading, whatever the status said: a keep counted
    # as applied when it never landed is recorded as settled and never retried.
    wanted = set(movie_ids)
    confirmed = sorted(m["id"] for m in radarr.movies() if m["id"] in wanted and tag_id in m.get("tags", []))
    unconfirmed = sorted(wanted - set(confirmed))
    note = "applied in a dry run (TAG_KEEPS_IN_DRY_RUN)" if dry_run else "applied"
    if unconfirmed:
        note = f"HTTP {status}; {len(unconfirmed)} not confirmed on re-read"
    return {
        "requested": len(movie_ids),
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
        movie_id = item.get("movie_id")
        if movie_id in standing:
            ledger.delete_proposal(movie_id)
            del standing[movie_id]
            lapsed += 1
    marked = 0
    for entry in plan.get("proposed") or []:
        stored = standing.get(entry["movie_id"]) or {}
        if (entry.get("proposal") or {}).get("by") != proposals.BY_PERSON or stored.get("by") == proposals.BY_PERSON:
            continue
        record = proposals.record(entry, "movie_id", run_id)
        ledger.write_proposal(record)
        standing[entry["movie_id"]] = record
        marked += 1
    return {"lapsed": lapsed, "marked": marked}


def record_verdicts(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    accepted: list[dict],
    movies: dict[int, dict],
    by_id: dict[int, dict],
    tag_labels: dict[int, str],
    monitored: set[int],
    ledger: Ledger,
    run_id: str,
    grace_days: int,
) -> tuple[list[dict], dict[str, int]]:
    """Put this run's verdicts on record, and collect what the judge sent to a person.

    A delete or a keep becomes a proposal that waits ``grace_days``; nothing is
    done about either in the run that made it. A review is recorded as before
    and returned, because the plan's review list was written before judgement:
    without carrying it out of here, a film the judge put in front of somebody
    would wait until next week's plan to be seen.
    """
    now = datetime.now(timezone.utc)
    escalated: list[dict] = []
    recorded = {proposals.DELETE: 0, proposals.KEEP: 0}
    for item in accepted:
        movie_id = item["movie_id"]
        if movie_id not in movies:
            continue
        if item["verdict"] in proposals.VERDICTS:
            proposal = proposals.judged(item["verdict"], item["reason"], now, grace_days)
            ledger.write_proposal(proposals.record({**by_id[movie_id], "proposal": proposal}, "movie_id", run_id))
            recorded[item["verdict"]] += 1
            continue
        film = to_film(movies[movie_id], tag_labels)
        escalated.append(
            {
                "movie_id": film.movie_id,
                "title": film.title,
                "year": film.year,
                "size_gb": film.size_gb,
                "reason": item["reason"],
            }
        )
        ledger.write_decision(
            build_decision_record(
                rehydrate(by_id[movie_id], film),
                verdict=VERDICT_REVIEW,
                reason=item["reason"],
                run_id=run_id,
                now=now,
                monitored=bool(film.tmdb_id and film.tmdb_id in monitored),
            )
        )
    return escalated, recorded


def settle_keeps(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    entries: list[dict],
    standing: dict[int, dict],
    movies: dict[int, dict],
    tag_labels: dict[int, str],
    monitored: set[int],
    ledger: Ledger,
    run_id: str,
    keep_days: int = proposals.DEFAULT_KEEP_DAYS,
) -> dict[str, Any]:
    """Close each due keep as a stored decision that spares the film for ``keep_days``.

    The judge never grants forever: no tag is written, so the film is judged again
    once the decision's date passes, or sooner when a fact it rested on changes.
    Each film is re-read first. A veto tag (``cleanup-keep``, ``keep``,
    ``keep-review``) already holds it for good, so its proposal closes with no
    decision. ``cleanup-eligible`` is a person's delete, which replaces the
    proposal at the next plan, so that one is left for it.
    """
    due = {entry["movie_id"]: entry for entry in entries if proposals.backed(entry, standing.get(entry["movie_id"]))}
    now = datetime.now(timezone.utc)
    kept: list[int] = []
    vetoed: list[int] = []
    left: list[int] = []
    for movie_id, entry in sorted(due.items()):
        record = movies.get(movie_id)
        if record is None:
            # The next plan finds it gone and lapses the proposal.
            continue
        film = to_film(record, tag_labels)
        if film.tags & VETO_TAGS:
            vetoed.append(movie_id)
            ledger.delete_proposal(movie_id)
            continue
        if TAG_HUMAN_ELIGIBLE in film.tags:
            left.append(movie_id)
            continue
        decision = build_decision_record(
            rehydrate(entry, film),
            verdict=VERDICT_SPARED,
            reason=str(entry["proposal"].get("reason") or ""),
            run_id=run_id,
            now=now,
            monitored=bool(film.tmdb_id and film.tmdb_id in monitored),
            reconsider_days=keep_days,
        )
        ledger.write_decision({**decision, "kept_until": decision["reconsider_after"]})
        ledger.delete_proposal(movie_id)
        kept.append(movie_id)
    return {
        "due": len(due),
        "kept": len(kept),
        "kept_ids": kept,
        "kept_until": (now + timedelta(days=keep_days)).date().isoformat() if kept else None,
        "keep_days": keep_days,
        "vetoed": vetoed,
        "left_for_person": left,
    }


def due_deletes(
    entries: list[dict],
    standing: dict[int, dict],
    movies: dict[int, dict],
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
        movie_id, proposal = entry["movie_id"], entry.get("proposal") or {}
        label = {key: entry.get(key) for key in ("movie_id", "title", "year", "size_gb")}
        record = movies.get(movie_id)
        if record is None:
            set_aside.append({**label, "why": "film is no longer in Radarr"})
            continue
        film = to_film(record, tag_labels)
        if proposal.get("by") == proposals.BY_PERSON:
            if TAG_HUMAN_ELIGIBLE not in film.tags:
                set_aside.append({**label, "why": "cleanup-eligible was removed since the plan"})
                continue
            marked.add(movie_id)
        elif not proposals.backed(entry, standing.get(movie_id)):
            set_aside.append({**label, "why": "no matching proposal is on record; not acted on"})
            continue
        assessment = rehydrate(entry, film, str(proposal.get("reason") or ""))
        assessment.marked_by_person = proposal.get("by") == proposals.BY_PERSON
        approved.append(assessment)
    return approved, set_aside, frozenset(marked)
