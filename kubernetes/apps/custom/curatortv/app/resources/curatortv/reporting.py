"""The weekly digest: what the run found, and what it needs from a person.

The run deletes nothing, so this is its entire output. A plan and a result left
on a volume report nothing to anyone, and the failure mode of a silent read-only
job is that it stops running and nobody notices for a month. So every run sends
something, including the runs where the answer is "nothing happened" -- silence
then means the job did not run, which is a fact worth knowing on its own.
"""

from __future__ import annotations

import html
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Pushover truncates the message at 1024 characters and the title at 250.
# Both are cut here instead, so the cut lands where it can be marked.
PUSH_LIMIT = 1000
TITLE_LIMIT = 240

# The push carries the shape of the week; the full lists are in the text report.
PUSH_LISTED = 4
TEXT_LISTED = 30


class NotificationError(RuntimeError):
    """The digest could not be delivered."""


@dataclass
class Digest:
    """One run, rendered for the two places it is read."""

    title: str
    push: str
    text: str


def _esc(value: Any) -> str:
    """Escape for the HTML subset Pushover renders."""
    return html.escape(str(value), quote=False)


def _label(item: dict) -> str:
    """``Title (Year)``, tolerating a record that carries neither."""
    title = item.get("title") or f"series {item.get('series_id', '?')}"
    year = item.get("year")
    return f"{title} ({year})" if year else str(title)


def _gb(items: list[dict]) -> float:
    """Total size of a set of series, in GB."""
    return sum(float(i.get("size_gb") or 0) for i in items)


def _reason(item: dict) -> str:
    """The short form of why a series needs a person.

    Cut at the first colon rather than matched against a table of known
    blockers: the planner owns that wording, and a table here would quietly stop
    matching the day someone rephrases one.
    """
    text = (item.get("blockers") or item.get("reasons") or [item.get("reason") or "no reason recorded"])[0]
    return str(text).split(":", 1)[0][:60]


def _tally(items: list[dict]) -> list[tuple[str, int, float]]:
    """Group series by reason, largest group first."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(_reason(item), []).append(item)
    counted = [(reason, len(shows), _gb(shows)) for reason, shows in groups.items()]
    return sorted(counted, key=lambda row: row[1], reverse=True)


def _age_hours(plan: dict, now: datetime) -> float | None:
    """How long ago the plan was written, or None if it does not say."""
    try:
        generated = datetime.fromisoformat(str(plan.get("generated_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (now - generated).total_seconds() / 3600


def _history_line(plan: dict) -> str:
    """Play-history coverage, which decides whether a zero play count means anything."""
    history = plan.get("history") or {}
    state = "complete" if history.get("complete") else "INCOMPLETE"
    return (
        f"{history.get('retrieved', '?')} rows, {state}, from {plan.get('history_coverage_start') or 'unknown'}"
        f", {plan.get('unattributed_plays')} plays unattributed"
    )


def _tokens(judgement: dict) -> str:
    """The judge's input and output token totals, when the record states both."""
    usage = judgement.get("usage")
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens")] if isinstance(usage, dict) else []
    if len(counts) != 2 or not all(isinstance(count, int) for count in counts):
        return ""
    return f"; {counts[0]:,} tokens in, {counts[1]:,} out"


def _judgement_line(judgement: dict | None) -> str:
    """One line on whether the model was asked, and what it used."""
    if not judgement:
        return "Judgement: no record written."
    status = judgement.get("status", "unknown")
    detail = judgement.get("detail") or ""
    if status in ("judged", "partial"):
        tally = judgement.get("verdicts") or {}
        verdicts = ", ".join(f"{count} {verdict}" for verdict, count in sorted(tally.items())) or "nothing"
        line = f"Judgement: {verdicts}{_tokens(judgement)}."
        # Some batches answered and some did not: the verdicts above are real, and incomplete.
        return line if status == "judged" else f"{line} INCOMPLETE: {detail}"
    return f"Judgement {status}: {detail}"


def _candidate_count(plan: dict) -> str:
    """How many candidates the plan found, and how many of them the judge was not asked about."""
    offered, deferred = (len(plan.get(key) or []) for key in ("candidates", "judge_deferred"))
    return f"{offered + deferred:,} candidates" + (f" · {deferred:,} deferred" if deferred else "")


def _deferred_line(plan: dict) -> list[str]:
    """Candidates the judge was not asked about this run, which is not a failure."""
    deferred = plan.get("judge_deferred") or []
    if not deferred:
        return []
    return [
        f"Deferred: {len(deferred):,} candidates ({_gb(deferred):,.0f} GB) are past the judge limit of "
        f"{plan.get('judge_limit', '?')} and wait for a later run."
    ]


def _day(stamp: Any) -> str:
    """A date as a person says it, or a fallback when the stamp cannot be read."""
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return "the next run"
    return f"{moment.day} {moment:%b}"


def _going(review: dict) -> str:
    """How many titles the next run takes, which is the one number a person has to act on."""
    count = len(review.get("going") or [])
    day = _day(review.get("next_run_at"))
    if not count:
        return f"nothing due on {day}"
    return f"{count} {'go' if review.get('mode') == 'act' else 'would go'} on {day}"


def _title(queue: list, review: dict | None, blocks: list, problems: list[str]) -> str:
    """The headline: a block first, then anything else that went wrong, then the ordinary week."""
    if blocks:
        return f"TV cleanup BLOCKED: {len(blocks)} issue" + ("s" if len(blocks) != 1 else "")
    if problems:
        return f"TV cleanup: {problems[0]}"
    if review is None:
        return f"TV cleanup: {len(queue)} to review"
    return f"TV cleanup: {_going(review)}" + (f" · {len(queue)} to review" if queue else "")


def _window_push(review: dict | None) -> list[str]:
    """The review window on the phone: what goes next, and what is waiting behind it."""
    if review is None:
        return []
    later, kept = len(review.get("later") or []), len(review.get("kept") or [])
    line = f"<b>{_esc(_going(review))}</b>" + (" unless you keep them" if review.get("going") else "")
    extras = [f"{later} waiting" if later else "", f"{kept} being kept" if kept else ""]
    return [line + "".join(f" · {extra}" for extra in extras if extra)]


def _proposal_lines(items: list[dict]) -> list[str]:
    """Every proposal with its size, due date and reason.

    Not cut at ``TEXT_LISTED``: with no review page this report is the only place a
    person can read what falls due, so every title it acts on has to be in it.
    """
    return [
        f"  - {_label(i)} {float(i.get('size_gb') or 0):.0f} GB, due {_day(i.get('due_at'))}: {i.get('reason')}"
        for i in items
    ]


def _window_lines(review: dict | None, result: dict | None, unpublished: str | None, unreported: str) -> list[str]:
    """The review window in the text report: the proposals on record and what this run added.

    The line about the review page appears only when one is configured (``unpublished`` is not None).
    """
    if review is None:
        if unreported:
            return [f"Review window: NOT REPORTED ({unreported})", ""]
        return [f"Review page: NOT UPDATED ({unpublished})", ""] if unpublished else []
    going, later, kept = (review.get(key) or [] for key in ("going", "later", "kept"))
    made = (result or {}).get("proposals") or {}
    recorded = made.get("recorded") or {}
    lines = [
        "## Review window",
        "",
        f"{_going(review).capitalize()}: {_gb(going):,.0f} GB (at most {review.get('cap')} a run)",
        *_proposal_lines(going),
        f"Waiting their turn or not yet due: {len(later)}",
        *_proposal_lines(later),
        f"Being kept for a year once their wait is over: {len(kept)}",
        *_proposal_lines(kept),
    ]
    if made:
        lines.append(
            f"This run: {recorded.get('delete', 0)} delete and {recorded.get('keep', 0)} keep proposed "
            f"(each waits {made.get('grace_days')} days), {made.get('marked_by_person', 0)} marked by a person, "
            f"{made.get('lapsed', 0)} lapsed"
        )
    if unpublished is not None:
        lines.append(f"Review page: NOT UPDATED ({unpublished})" if unpublished else "Review page: updated")
    return [*lines, ""]


def current_plan(plan: dict | None, now: datetime, stale_after_hours: float = 12.0) -> tuple[dict | None, list[str]]:
    """This run's plan if it has one, and the reasons the run acted on nothing.

    For the review page, which has to say why nothing is going when a run was
    blocked or never planned.
    """
    if plan is None:
        return None, ["no plan was written for this run"]
    age = _age_hours(plan, now)
    if age is None:
        return None, ["the plan carries no usable date"]
    if age > stale_after_hours:
        return None, [f"the newest plan is {age:.0f}h old, so this run's planner did not write one"]
    return plan, [str(block) for block in plan.get("blocks") or []]


def _nothing_to_report(reason: str, detail: str, window: tuple) -> Digest:
    """A run that produced no plan still has to say so, and still lists what falls due.

    ``window`` is the review document, ``unpublished`` and ``unreported``, as
    ``render`` takes them: the proposals on record do not depend on this run's plan.
    """
    page, unpublished, unreported = window
    push = [f"<b>{_esc(reason)}</b>", _esc(detail), *_window_push(page)]
    push += [f"· {_esc(issue)}" for issue in _problems([], {}, None, unpublished, unreported)]
    lines = _window_lines(page, None, unpublished, unreported)
    return Digest(
        title=f"TV cleanup: {reason}",
        push=_clip("\n".join(push)),
        text="\n".join(["# TV cleanup", "", reason, "", detail, "", *lines]).rstrip("\n") + "\n",
    )


def _execution_lines(result: dict | None) -> list[str]:
    """What execute did, in the order that matters if it went wrong."""
    if result is None:
        return ["Execution: no result file; the execute step did not finish."]
    lines = []
    for key in ("failed", "uncertain"):
        items = result.get(key) or []
        if items:
            lines.append(f"{key.upper()}: {len(items)}")
            lines.extend(f"  - {_label(i)}: {i.get('why') or i.get('http')}" for i in items)
    deleted = result.get("deleted") or []
    simulated = all(i.get("simulated") for i in deleted) if deleted else True
    verb = "Would delete" if simulated else "Deleted"
    lines.append(f"{verb}: {len(deleted)} ({_gb(deleted):.0f} GB)")
    lines.extend(f"  - {_label(i)} {float(i.get('size_gb') or 0):.0f} GB" for i in deleted[:TEXT_LISTED])
    skipped = result.get("skipped") or []
    if skipped:
        lines.append(f"Skipped at the last check: {len(skipped)}")
        lines.extend(f"  - {_label(i)}: {i.get('why')}" for i in skipped)
    deferred = result.get("deferred") or []
    if deferred:
        lines.append(f"Held back by the per-run cap: {len(deferred)}")
    rejected = result.get("rejected_recommendations") or []
    if rejected:
        lines.append(f"Rejected recommendations: {len(rejected)}")
    return lines


def _judge_keep_lines(result: dict | None) -> list[str]:
    """The judge's keeps that fell due: each is kept for a year, then judged again. No tag is written."""
    kept = (result or {}).get("judge_keeps") or {}
    vetoed, left = len(kept.get("vetoed") or []), len(kept.get("left_for_person") or [])
    if not (kept.get("due") or vetoed or left):
        return []
    lines = [f"## Kept for a year by the judge: {kept.get('kept', 0)} of {kept.get('due', 0)} due"]
    if kept.get("kept"):
        lines.append(f"Judged again after {kept.get('kept_until')}, or sooner if a fact changes; no tag is written.")
    if vetoed:
        lines.append(f"{vetoed} already kept for good by a tag; their proposals closed.")
    if left:
        lines.append(f"{left} marked cleanup-eligible by a person, whose delete replaces the keep.")
    return [*lines, ""]


def _problems(
    blocks: list, result: dict | None, judgement: dict | None, unpublished: str | None = None, unreported: str = ""
) -> list[str]:
    """Everything that should stop this reading as a quiet week.

    The phone is where the distinction is most often lost: a judgement that
    failed leaves an empty recommendation list, which then executes cleanly and
    reports "0 deleted" -- indistinguishable from a week with nothing to do.
    """
    problems = [str(b) for b in blocks]
    # A list a person may be about to act on is a week stale: said whatever else happened.
    page = [f"review page not updated: {unpublished}"[:120]] if unpublished else []
    if unreported:
        page.append(f"review window not reported: {unreported}"[:120])
    if result is None:
        return [*problems, "execute did not finish", *page]
    status = (judgement or {}).get("status")
    if status not in (None, "judged", "not asked"):
        problems.append(f"judgement {status}: {(judgement or {}).get('detail', '')}"[:120])
    for key in ("failed", "uncertain"):
        count = len(result.get(key) or [])
        if count:
            problems.append(f"{count} deletion{'s' if count != 1 else ''} {key}")
    untagged = len((result.get("keep_tags_added") or {}).get("unconfirmed") or [])
    if untagged:
        problems.append(f"{untagged} keep tag{'s' if untagged != 1 else ''} not confirmed")
    unavailable = sum(1 for item in result.get("skipped") or [] if item.get("unavailable"))
    if unavailable:
        problems.append(
            f"{unavailable} deletion{'s' if unavailable != 1 else ''} skipped: a safety check was unavailable"
        )
    unjudged = len(result.get("unjudged") or [])
    if unjudged:
        problems.append(f"{unjudged} candidate{'s' if unjudged != 1 else ''} came back with no verdict")
    return [*problems, *page]


def _clip(message: str) -> str:
    """Cut to the push limit at a line boundary, and say that it was cut."""
    if len(message) <= PUSH_LIMIT:
        return message
    kept = message[:PUSH_LIMIT].rsplit("\n", 1)[0]
    return f"{kept}\n<i>(truncated)</i>"


def _this_weeks(plan: dict, result: dict | None, judgement: dict | None) -> tuple[dict | None, dict | None]:
    """Discard anything on the volume that belongs to an earlier run.

    Every file here outlives the week that wrote it, and the two CronJobs
    generate their own run ids -- so each output names the *plan* it belongs to,
    and that is what ties them together. A leftover would otherwise report an
    execution that did not happen and a judge that was never reached as though
    both had answered this morning.
    """
    run_id = plan.get("run_id")
    if result is not None and result.get("plan_run_id") != run_id:
        result = None
    if judgement is not None and judgement.get("plan_run_id") != run_id:
        judgement = {"status": "failed", "detail": "no judgement was recorded for this plan"}
    return result, judgement


def render(
    plan: dict | None,
    result: dict | None,
    judgement: dict | None,
    now: datetime,
    stale_after_hours: float = 12.0,
    page: dict | None = None,
    unpublished: str | None = None,
    unreported: str = "",
) -> Digest:
    """Build the digest for one run.

    ``page`` is the review document, built from the proposals on record whether
    or not it is published. ``unpublished`` is None when no review page is
    configured, empty when the document reached it, and otherwise why it did
    not. ``unreported`` says why
    there is no document at all when no page is configured: the proposals that
    fall due could not be read, which is a problem whether or not anything is
    published.
    """
    if plan is None:
        return _nothing_to_report(
            "no plan",
            "The curatortv job writes the plan this reads. Check whether it ran.",
            (page, unpublished, unreported),
        )

    age = _age_hours(plan, now)
    if age is None:
        return _nothing_to_report(
            "plan unreadable", "The plan carries no usable generated_at.", (page, unpublished, unreported)
        )
    if age > stale_after_hours:
        return _nothing_to_report(
            "plan is stale",
            f"The newest plan is {age:.0f}h old, so this week's curatortv job did not produce one.",
            (page, unpublished, unreported),
        )

    result, judgement = _this_weeks(plan, result, judgement)

    mode = str(plan.get("mode", "?"))
    counts = plan.get("counts") or {}
    review = plan.get("review") or []
    missing = plan.get("missing_file") or []
    blocks = plan.get("blocks") or []
    qualify = len(plan.get("keep_tag_additions") or [])
    keep_tags = (result or {}).get("keep_tags_added") or {}
    library = sum(int(v) for v in counts.values())
    # The judge can send a series to a person rather than to deletion, and that
    # decision is made after the plan's own review list was written. The plan's list
    # holds only what the engine could not assess and reviews the judge asked for earlier.
    escalated = (result or {}).get("escalated") or []
    queue = review + escalated

    problems = _problems(blocks, result, judgement, unpublished, unreported)
    title = _title(queue, page, blocks, problems)

    push = [f"<b>{_esc(mode.upper())}</b> · {library:,} series · {_candidate_count(plan)}"]
    if problems:
        push.append("<b>Needs attention:</b>")
        push.extend(f"· {_esc(issue)}" for issue in problems[:PUSH_LISTED])
    if queue:
        push.append(f"<b>{len(queue)} waiting on you</b> · {_gb(queue):,.0f} GB")
        push.extend(f"· {count} × {_esc(reason)}" for reason, count, _ in _tally(queue)[:PUSH_LISTED])
    if result:
        deleted = result.get("deleted") or []
        verb = "would delete" if all(i.get("simulated") for i in deleted) else "DELETED"
        push.append(f"{verb} {len(deleted)} · {_gb(deleted):,.0f} GB")
    push += _window_push(page)
    if missing:
        push.append(f"{len(missing)} series have no episode on disk")

    text = [
        "# TV cleanup",
        "",
        f"Run {plan.get('run_id')} · mode {mode} · planned {age:.1f}h ago",
        "",
        f"Library: {library:,} series — " + ", ".join(f"{v:,} {k}" for k, v in sorted(counts.items())),
        f"Recovery: {plan.get('recycle_bin')}",
        f"Play history: {_history_line(plan)}",
        _judgement_line(judgement),
        *_deferred_line(plan),
        "",
    ]
    if blocks:
        text += ["## Blocked", "", *(f"- {b}" for b in blocks), ""]
        text += [
            "If a fall listed above is expected, accept this run's figures as the new baseline by setting "
            f"CLEANUP_ACCEPT_BASELINE={plan.get('run_id')} on the planner (curatortv) for its next run.",
            "",
        ]
    text += ["## Execution", "", *_execution_lines(result), ""]
    text += _window_lines(page, result, unpublished, unreported)
    if qualify or keep_tags:
        applied = keep_tags.get("applied")
        outcome = f"{applied} applied" if applied is not None else "execute did not reach it"
        text += [f"## Keep tag: {qualify} qualify, {outcome}", f"{keep_tags.get('note', '')}".strip(), ""]
    text += _judge_keep_lines(result)
    if queue:
        text += [f"## Waiting on you: {len(queue)} series, {_gb(queue):,.0f} GB", ""]
        text += [f"- {count} × {reason} ({size:,.0f} GB)" for reason, count, size in _tally(queue)]
        text += ["", "Largest:", ""]
        largest = sorted(queue, key=lambda i: float(i.get("size_gb") or 0), reverse=True)
        text += [f"- {_label(i)} {float(i.get('size_gb') or 0):.0f} GB — {_reason(i)}" for i in largest[:TEXT_LISTED]]
        text += [""]
    if escalated:
        text += [f"## Sent to you by the judge: {len(escalated)}", ""]
        text += [f"- {_label(i)} {float(i.get('size_gb') or 0):.0f} GB — {i.get('reason')}" for i in escalated]
        text += [""]
    if missing:
        text += [f"## No episode on disk: {len(missing)}", ""]
        text += [f"- {_label(i)}" for i in missing[:TEXT_LISTED]]
        text += [""]

    return Digest(title=title[:TITLE_LIMIT], push=_clip("\n".join(push)), text="\n".join(text) + "\n")


def post(url: str, token: str, digest: Digest, opener=None, timeout: int = 20, link: str = "") -> str:
    """Deliver the digest to the webhook. Raises rather than reporting silence as success.

    ``link`` is where the push should open, sent as ``url`` only when there is one.
    """
    fields = {"title": digest.title, "message": digest.push, **({"url": link} if link else {})}
    payload = json.dumps(fields).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "X-Chaski-Token": token},
        method="POST",
    )
    try:
        with (opener or urllib.request.build_opener()).open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise NotificationError(f"webhook returned {response.status}")
            return f"digest delivered, HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        raise NotificationError(f"webhook returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotificationError(f"webhook unreachable: {exc}") from exc


def read(path) -> dict | None:
    """Load a JSON document, treating absent and unreadable alike as absent."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


__all__ = ["Digest", "NotificationError", "current_plan", "post", "read", "render"]
