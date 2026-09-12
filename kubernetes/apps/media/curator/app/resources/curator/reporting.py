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
    title = item.get("title") or f"movie {item.get('movie_id', '?')}"
    year = item.get("year")
    return f"{title} ({year})" if year else str(title)


def _gb(items: list[dict]) -> float:
    """Total size of a set of films, in GB."""
    return sum(float(i.get("size_gb") or 0) for i in items)


def _reason(item: dict) -> str:
    """The short form of why a film needs a person.

    Cut at the first colon rather than matched against a table of known
    blockers: the planner owns that wording, and a table here would quietly stop
    matching the day someone rephrases one.
    """
    text = (item.get("blockers") or item.get("reasons") or [item.get("reason") or "no reason recorded"])[0]
    return str(text).split(":", 1)[0][:60]


def _tally(items: list[dict]) -> list[tuple[str, int, float]]:
    """Group films by reason, largest group first."""
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(_reason(item), []).append(item)
    counted = [(reason, len(films), _gb(films)) for reason, films in groups.items()]
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


def _judgement_line(judgement: dict | None) -> str:
    """One line on whether the model was asked, and what it cost."""
    if not judgement:
        return "Judgement: no record written."
    status = judgement.get("status", "unknown")
    detail = judgement.get("detail") or ""
    if status == "judged":
        tally = judgement.get("verdicts") or {}
        verdicts = ", ".join(f"{count} {verdict}" for verdict, count in sorted(tally.items())) or "nothing"
        cost = judgement.get("cost_usd")
        return f"Judgement: {verdicts}" + (f", ${cost}" if cost else "") + "."
    return f"Judgement {status}: {detail}"


def _nothing_to_report(reason: str, detail: str) -> Digest:
    """A run that produced no plan still has to say so."""
    return Digest(
        title=f"Library cleanup: {reason}",
        push=f"<b>{_esc(reason)}</b>\n{_esc(detail)}",
        text=f"# Library cleanup\n\n{reason}\n\n{detail}\n",
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


def _problems(blocks: list, result: dict | None, judgement: dict | None) -> list[str]:
    """Everything that should stop this reading as a quiet week.

    The phone is where the distinction is most often lost: a judgement that
    failed leaves an empty recommendation list, which then executes cleanly and
    reports "0 deleted" -- indistinguishable from a week with nothing to do.
    """
    problems = [str(b) for b in blocks]
    if result is None:
        problems.append("execute did not finish")
        return problems
    status = (judgement or {}).get("status")
    if status not in (None, "judged", "not asked"):
        problems.append(f"judgement {status}: {(judgement or {}).get('detail', '')}"[:120])
    for key in ("failed", "uncertain"):
        count = len(result.get(key) or [])
        if count:
            problems.append(f"{count} deletion{'s' if count != 1 else ''} {key}")
    unjudged = len(result.get("unjudged") or [])
    if unjudged:
        problems.append(f"{unjudged} candidate{'s' if unjudged != 1 else ''} came back with no verdict")
    return problems


def _clip(message: str) -> str:
    """Cut to the push limit at a line boundary, and say that it was cut."""
    if len(message) <= PUSH_LIMIT:
        return message
    kept = message[:PUSH_LIMIT].rsplit("\n", 1)[0]
    return f"{kept}\n<i>(truncated)</i>"


def render(
    plan: dict | None,
    result: dict | None,
    judgement: dict | None,
    now: datetime,
    stale_after_hours: float = 12.0,
) -> Digest:
    """Build the digest for one run."""
    if plan is None:
        return _nothing_to_report(
            "no plan",
            "The curator job writes the plan this reads. Check whether it ran.",
        )

    age = _age_hours(plan, now)
    if age is None:
        return _nothing_to_report("plan unreadable", "The plan carries no usable generated_at.")
    if age > stale_after_hours:
        return _nothing_to_report(
            "plan is stale",
            f"The newest plan is {age:.0f}h old, so this week's curator job did not produce one.",
        )

    # Both files sit on the same volume from one week to the next, and the two
    # CronJobs generate their own run ids -- so the result names the plan it
    # acted on, and that is what ties them together. Anything else is an
    # execution that did not happen this week.
    if result is not None and result.get("plan_run_id") != plan.get("run_id"):
        result = None

    mode = str(plan.get("mode", "?"))
    counts = plan.get("counts") or {}
    review = plan.get("review") or []
    missing = plan.get("missing_file") or []
    candidates = plan.get("candidates") or []
    blocks = plan.get("blocks") or []
    qualify = len(plan.get("keep_tag_additions") or [])
    keep_tags = (result or {}).get("keep_tags_added") or {}
    library = sum(int(v) for v in counts.values())
    # The judge can send a film to a person rather than to deletion, and that
    # decision is made after the plan's own review list was written.
    escalated = (result or {}).get("escalated") or []
    queue = review + escalated

    problems = _problems(blocks, result, judgement)

    title = f"Library cleanup: {len(queue)} to review"
    if blocks:
        title = f"Library cleanup BLOCKED: {len(blocks)} issue" + ("s" if len(blocks) != 1 else "")
    elif problems:
        title = f"Library cleanup: {problems[0]}"

    push = [f"<b>{_esc(mode.upper())}</b> · {library:,} films · {len(candidates)} candidates"]
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
    if missing:
        push.append(f"{len(missing)} monitored films have no file")

    text = [
        "# Library cleanup",
        "",
        f"Run {plan.get('run_id')} · mode {mode} · planned {age:.1f}h ago",
        "",
        f"Library: {library:,} films — " + ", ".join(f"{v:,} {k}" for k, v in sorted(counts.items())),
        f"Recovery: {plan.get('recycle_bin')}",
        f"Play history: {_history_line(plan)}",
        _judgement_line(judgement),
        "",
    ]
    if blocks:
        text += ["## Blocked", "", *(f"- {b}" for b in blocks), ""]
    text += ["## Execution", "", *_execution_lines(result), ""]
    if qualify or keep_tags:
        applied = keep_tags.get("applied")
        outcome = f"{applied} applied" if applied is not None else "execute did not reach it"
        text += [f"## Keep tag: {qualify} qualify, {outcome}", f"{keep_tags.get('note', '')}".strip(), ""]
    if queue:
        text += [f"## Waiting on you: {len(queue)} films, {_gb(queue):,.0f} GB", ""]
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
        text += [f"## Monitored with no file: {len(missing)}", ""]
        text += [f"- {_label(i)}" for i in missing[:TEXT_LISTED]]
        text += [""]

    return Digest(title=title[:TITLE_LIMIT], push=_clip("\n".join(push)), text="\n".join(text) + "\n")


def post(url: str, token: str, digest: Digest, opener=None, timeout: int = 20) -> str:
    """Deliver the digest to the webhook. Raises rather than reporting silence as success."""
    payload = json.dumps({"title": digest.title, "message": digest.push}).encode()
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


__all__ = ["Digest", "NotificationError", "post", "read", "render"]
