#!/usr/bin/env python3
"""Turn the engine's plan into batches for the judge, or decide not to ask.

Runs before the judge container. The plan's candidates are already the ones
chosen for this run, largest first; they are split into batches of
``CLEANUP_JUDGE_BATCH``, one request each, so an answer that fails costs one
batch and not the whole run.

Written to /work when there is something to judge:

- ``candidates-NNN.json``, one per batch: the records the judge is sent, as-is;
- ``batches.json``: the candidates each batch holds and the plan they came from,
  so the validator can refuse anything that is not this run's and can count the
  titles of a batch that failed.

Otherwise /work/skip is written -- the judge container checks for that marker
and exits without calling anything.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLAN = Path(os.environ.get("PLAN_PATH", "/state/cleanup-plan/plan.json"))
WORK = Path(os.environ.get("WORK_DIR", "/work"))
MAX_AGE_HOURS = float(os.environ.get("CLEANUP_MAX_PLAN_AGE_HOURS", "6"))
DEFAULT_BATCH = 50

ID_FIELD = "movie_id"

# Only what judgement needs. Identifiers the model has no use for stay out.
FIELDS = (
    "movie_id",
    "title",
    "year",
    "imdb_score",
    "imdb_votes",
    "size_gb",
    "days_available",
    "play_count",
    "distinct_completers",
    "origin",
    "requested_by",
    "collection",
    "siblings_owned",
    "subject_counts",
    "genres",
    "studio",
    "original_language",
    "overview",
)

# Everything a previous attempt in the same work directory could have left behind.
STALE = ("candidates-*.json", "result-*.json", "batches.json", "judge-failed", "skip")


def skip(reason: str) -> int:
    """Record that no judgement should be asked for, and why."""
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "skip").write_text(reason, encoding="utf-8")
    print(f"not judging: {reason}")
    return 0


def batch_size() -> int | None:
    """Candidates per call, or None when the setting cannot be used."""
    raw = os.environ.get("CLEANUP_JUDGE_BATCH", "").strip()
    if not raw:
        return DEFAULT_BATCH
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def clear_stale() -> None:
    """Remove what an earlier attempt left in the work directory."""
    WORK.mkdir(parents=True, exist_ok=True)
    for pattern in STALE:
        for leftover in WORK.glob(pattern):
            leftover.unlink()


def write_batches(run_id, candidates: list[dict], size: int) -> int:
    """One candidates file per batch, plus the list that names them. Returns the batch count."""
    batches = []
    for index, start in enumerate(range(0, len(candidates), size), start=1):
        name = f"{index:03d}"
        end = start + size
        chunk = candidates[start:end]
        trimmed = [{k: c.get(k) for k in FIELDS} for c in chunk]
        (WORK / f"candidates-{name}.json").write_text(json.dumps(trimmed, indent=1) + "\n", encoding="utf-8")
        batches.append({"name": name, "ids": [c.get(ID_FIELD) for c in chunk]})

    (WORK / "batches.json").write_text(
        json.dumps({"plan_run_id": run_id, "id_field": ID_FIELD, "batches": batches}, indent=1),
        encoding="utf-8",
    )
    return len(batches)


def main() -> int:
    """Write the batches, or leave a skip marker."""
    clear_stale()
    if not PLAN.is_file():
        return skip(f"no plan at {PLAN}; did the curatorfilm job run?")

    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # An interrupted planner leaves a half-written file; raising here stops the pod at the
        # first init container, so a failed plan produces silence instead of the promised digest.
        return skip(f"plan at {PLAN} could not be read: {exc}")

    # A plan from a failed earlier schedule describes a library that's moved on. The executor
    # checks this too; checking here avoids paying for a judgement that would be refused anyway.
    generated = plan.get("generated_at")
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return skip(f"plan has no usable generated_at ({generated!r})")
    if age > timedelta(hours=MAX_AGE_HOURS):
        return skip(f"plan is {age.total_seconds() / 3600:.1f}h old, over the {MAX_AGE_HOURS}h limit")

    if plan.get("blocks"):
        return skip("plan recorded blocking issues: " + "; ".join(plan["blocks"]))

    candidates = plan.get("candidates") or []
    if not candidates:
        return skip("no candidates")

    size = batch_size()
    if size is None:
        # Skipped, not raised, for the same reason as an unreadable plan: the digest must go out.
        return skip(f"CLEANUP_JUDGE_BATCH must be a whole number above zero, not {os.environ['CLEANUP_JUDGE_BATCH']!r}")

    count = write_batches(plan.get("run_id"), candidates, size)
    print(f"{count} batch(es) written for {len(candidates)} candidates, {size} to a batch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
