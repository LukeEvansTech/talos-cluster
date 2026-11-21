"""The review document: what a person can still overrule, and how the run went.

One JSON document per library and run. It lists every verdict that is waiting,
what needs a person and what this run deleted, and summarises the plan, the
judge, the executor and the digest. ``report`` builds it on every run and posts
it to a receiver only when one is configured; the contract is in the README.

Both engines share this file unchanged; the id field's name is passed in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from . import proposals

VERSION = 2
SIGNATURE_HEADER = "X-Curator-Signature"

# The page shows these as written; cut here so one long synopsis cannot bloat the document.
TITLE_LIMIT = 300
OVERVIEW_LIMIT = 400
REASON_LIMIT = 600

# What the page may offer for a title: `keep` adds cleanup-keep, `delete` adds cleanup-eligible.
ACT_KEEP = "keep"
ACT_DELETE = "delete"


class PublishError(RuntimeError):
    """The review document did not reach the page."""


def _text(value: Any, limit: int) -> str:
    """A string field, cut to its limit; anything that is not a string is dropped."""
    return value.strip()[:limit] if isinstance(value, str) else ""


def _number(value: Any) -> int | float | None:
    """A numeric field, or None; a bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _int(value: Any) -> int | None:
    """An integer field, or None; a bool is not a count here."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _length(value: Any) -> int:
    """How many entries a list field holds; anything else holds none."""
    return len(value) if isinstance(value, list) else 0


def _item(item_id: int, facts: dict, detail: dict) -> dict[str, Any]:
    """One title as the page shows it: the facts a verdict was given on, and the verdict."""
    finished = facts.get("distinct_completers", facts.get("distinct_viewers"))
    return {
        "id": item_id,
        "title": _text(facts.get("title"), TITLE_LIMIT) or f"#{item_id}",
        "year": _number(facts.get("year")),
        "size_gb": _number(facts.get("size_gb")),
        "imdb_score": _number(facts.get("imdb_score")),
        "imdb_votes": _number(facts.get("imdb_votes")),
        "plays": _number(facts.get("play_count")),
        "finished_by": _number(finished),
        "origin": _text(facts.get("origin"), 40),
        "requested_by": _text(facts.get("requested_by"), 80),
        "overview": _text(facts.get("overview"), OVERVIEW_LIMIT),
        "verdict": _text(detail.get("verdict"), 20),
        "by": _text(detail.get("by"), 20),
        "reason": _text(detail.get("reason"), REASON_LIMIT),
        "proposed_at": _text(detail.get("proposed_at"), 40),
        "due_at": _text(detail.get("due_at"), 40),
        "simulated": detail.get("simulated") is True,
        "actions": list(detail.get("actions") or []),
    }


def _proposal_item(record: dict, id_key: str) -> dict[str, Any]:
    """A stored proposal. A delete can be kept; a keep can be deleted instead."""
    actions = [ACT_KEEP] if record.get("verdict") == proposals.DELETE else [ACT_DELETE]
    facts = {**(record.get("entry") or {}), "title": record.get("title") or (record.get("entry") or {}).get("title")}
    return _item(record[id_key], facts, {**record, "actions": actions})


def _size(record: dict) -> float:
    """Size on disk as the verdict saw it, for largest-first ordering."""
    return float(_number((record.get("entry") or {}).get("size_gb")) or 0)


def _needs_a_person(plan: dict | None, result: dict | None, id_key: str, taken: set[int]) -> list[dict]:
    """What only a person can settle: the judge's reviews, and titles the engine cannot assess.

    A title the engine could not assess can be kept from the page but not deleted
    from it: ``cleanup-eligible`` does not override the check that stopped it.
    """
    items = []
    for entry in (plan or {}).get("review") or []:
        blockers = entry.get("blockers") or []
        detail = {
            "verdict": "review",
            "by": "engine" if blockers else proposals.BY_JUDGE,
            "reason": (blockers or entry.get("reasons") or [""])[0],
            "actions": [ACT_KEEP] if blockers else [ACT_KEEP, ACT_DELETE],
        }
        items.append((entry.get(id_key), entry, detail))
    for entry in (result or {}).get("escalated") or []:
        detail = {
            "verdict": "review",
            "by": proposals.BY_JUDGE,
            "reason": entry.get("reason"),
            "actions": [ACT_KEEP, ACT_DELETE],
        }
        items.append((entry.get(id_key), entry, detail))

    out = []
    for item_id, entry, detail in items:
        # A proposal the plan has turned away is still on record until execute runs; it is
        # shown once, as the proposal, so the page never holds two answers for one title.
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id in taken:
            continue
        taken.add(item_id)
        out.append(_item(item_id, entry, detail))
    return sorted(out, key=lambda item: float(item["size_gb"] or 0), reverse=True)


def _plan_summary(plan: dict | None) -> dict[str, Any] | None:
    """The plan's funnel: every title by outcome, and how the candidates were split."""
    if plan is None:
        return None
    counts = {str(key): count for key, count in (plan.get("counts") or {}).items() if _int(count) is not None}
    return {
        "counts": dict(sorted(counts.items())),
        "titles": sum(counts.values()),
        "offered": _length(plan.get("candidates")),
        "deferred": _length(plan.get("judge_deferred")),
        "judge_limit": _int(plan.get("judge_limit")),
        "proposed": _length(plan.get("proposed")),
        "lapsed": _length(plan.get("lapsed_proposals")),
        "review": _length(plan.get("review")),
        "missing_file": _length(plan.get("missing_file")),
    }


def _judge_summary(plan: dict | None, judgement: dict | None) -> dict[str, Any]:
    """Whether the judge answered, in how many batches, at what cost in tokens, and as which model group."""
    if judgement is not None and (plan is None or judgement.get("plan_run_id") != plan.get("run_id")):
        judgement = {"status": "failed", "detail": "no judgement was recorded for this plan"}
    if judgement is None:
        judgement = {"status": "missing", "detail": "no judgement record was written"}
    batches = judgement.get("batches")
    batches = batches if isinstance(batches, dict) else {}
    usage = judgement.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    failed = batches.get("failed")
    return {
        "status": _text(judgement.get("status"), 20) or "missing",
        "detail": _text(judgement.get("detail"), REASON_LIMIT),
        "batches": _int(batches.get("asked")),
        "failed_batches": len(failed) if isinstance(failed, list) else None,
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "model": _text(judgement.get("model"), 100) or None,
    }


def _executor_summary(result: dict | None) -> dict[str, Any] | None:
    """What execute did with this plan: deleted or would have, skipped, failed, tagged."""
    if result is None:
        return None
    deleted = [item for item in result.get("deleted") or [] if isinstance(item, dict)]
    tagged = 0
    for key in ("keep_tags_added", "judge_keeps_tagged"):
        record = result.get(key)
        tagged += (_int(record.get("applied")) or 0) if isinstance(record, dict) else 0
    return {
        "deleted": sum(1 for item in deleted if item.get("simulated") is not True),
        "would_delete": sum(1 for item in deleted if item.get("simulated") is True),
        "size_gb": round(sum(float(_number(item.get("size_gb")) or 0) for item in deleted), 1),
        "skipped": _length(result.get("skipped")),
        "failed": _length(result.get("failed")),
        "uncertain": _length(result.get("uncertain")),
        "deferred": _length(result.get("deferred")),
        "keep_tags_applied": tagged,
    }


def digest_record(title: str, message: str, text: str) -> dict[str, str]:
    """The digest as it was sent: the push title and message, and the full text report."""
    return {"title": title, "message": message, "text": text}


def build(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    library: str,
    id_key: str,
    plan: dict | None,
    result: dict | None,
    records: dict[int, dict],
    now: datetime,
    run: dict[str, Any],
    judgement: dict | None = None,
    digest: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the document for one library.

    ``plan`` is None when this run has no current plan; ``records`` is the
    ledger's proposals; ``run`` carries the deletion cap, the mode and the
    reasons this run acted on nothing; ``digest`` is the digest as sent, from
    ``digest_record``. Deletes the next run will find due are listed in the
    order it takes them (a person's first, then largest), cut at the cap; the
    rest are listed as waiting.
    """
    # A result left on the volume by an earlier run describes that run, not this one.
    if result is not None and (plan is None or result.get("plan_run_id") != plan.get("run_id")):
        result = None
    next_run = proposals.next_run(proposals.when((plan or {}).get("generated_at")), now)

    stored = [record for record in records.values() if isinstance(record.get(id_key), int)]
    deletes = [record for record in stored if record.get("verdict") == proposals.DELETE]
    keeps = [record for record in stored if record.get("verdict") == proposals.KEEP]
    due = sorted(
        (record for record in deletes if proposals.is_due(record, next_run)),
        key=lambda record: (record.get("by") == proposals.BY_PERSON, _size(record)),
        reverse=True,
    )
    waiting = sorted((record for record in deletes if not proposals.is_due(record, next_run)), key=_size, reverse=True)
    cap = max(int(run.get("cap") or 0), 0)
    taken = {record[id_key] for record in stored}

    return {
        "version": VERSION,
        "library": library,
        "generated_at": now.isoformat(),
        "run_id": _text((plan or {}).get("run_id"), 100),
        "mode": "act" if run.get("mode") == "act" else "dry-run",
        "blocks": [str(block)[:REASON_LIMIT] for block in run.get("blocks") or []],
        "cap": cap,
        "next_run_at": next_run.isoformat(),
        "going": [_proposal_item(record, id_key) for record in due[:cap]],
        "later": [_proposal_item(record, id_key) for record in due[cap:] + waiting],
        "kept": [_proposal_item(record, id_key) for record in sorted(keeps, key=_size, reverse=True)],
        "needs_you": _needs_a_person(plan, result, id_key, taken),
        "deleted": [
            _item(entry[id_key], entry, {"verdict": proposals.DELETE, "simulated": entry.get("simulated") is True})
            for entry in (result or {}).get("deleted") or []
            if isinstance(entry.get(id_key), int) and not isinstance(entry.get(id_key), bool)
        ],
        "summary": {
            "plan": _plan_summary(plan),
            "judge": _judge_summary(plan, judgement),
            "executor": _executor_summary(result),
            "digest": digest,
        },
    }


def sign(key: str, library: str, body: bytes) -> str:
    """The signature the page checks a document against.

    Keyed with the library's API key, which the page holds too, so the key itself
    never travels and a document cannot be passed off as the other library's.
    """
    if not key:
        raise PublishError("no key to sign the review document with")
    digest = hmac.new(key.encode(), library.encode() + b"\n" + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def encode(document: dict) -> bytes:
    """The exact bytes that are sent and signed: compact JSON with sorted keys."""
    return json.dumps(document, separators=(",", ":"), sort_keys=True).encode()


def publish(url: str, library: str, key: str, document: dict, opener=None, timeout: int = 30) -> str:
    """Send the document to the receiver. Raises rather than reporting silence as success."""
    body = encode(document)
    request = urllib.request.Request(
        f"{url.rstrip('/')}/publish/{library}",
        data=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(key, library, body)},
        method="POST",
    )
    try:
        with (opener or urllib.request.build_opener()).open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise PublishError(f"review page returned {response.status}")
            return f"review page updated, HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # The error holds the response open; nothing reads its body, so release it here.
        exc.close()
        raise PublishError(f"review page returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishError(f"review page unreachable: {exc}") from exc


__all__ = ["PublishError", "SIGNATURE_HEADER", "VERSION", "build", "digest_record", "encode", "publish", "sign"]
