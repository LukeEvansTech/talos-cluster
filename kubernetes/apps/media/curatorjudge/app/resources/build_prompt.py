#!/usr/bin/env python3
"""Turn the engine's plan into a prompt for the judge, or decide not to ask.

Runs in the Python image before the judge container, so no JSON tooling has to
exist in the Node image. Writes /work/prompt.txt when there is something to
judge, and /work/skip when there is not -- the judge container checks for that
marker and exits without calling anything.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLAN = Path(os.environ.get("PLAN_PATH", "/state/cleanup-plan/plan.json"))
TASTE = Path(os.environ.get("TASTE_PATH", "/prompt/taste.md"))
WORK = Path(os.environ.get("WORK_DIR", "/work"))
MAX_AGE_HOURS = float(os.environ.get("CLEANUP_MAX_PLAN_AGE_HOURS", "6"))

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
    "collection",
    "siblings_owned",
    "subject_counts",
    "genres",
    "studio",
    "overview",
)


def skip(reason: str) -> int:
    """Record that no judgement should be asked for, and why."""
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "skip").write_text(reason, encoding="utf-8")
    print(f"not judging: {reason}")
    return 0


def main() -> int:
    """Build the prompt, or leave a skip marker."""
    if not PLAN.is_file():
        return skip(f"no plan at {PLAN}; did the curator job run?")

    plan = json.loads(PLAN.read_text(encoding="utf-8"))

    # A plan left behind by a failed earlier schedule describes a library that
    # has moved on. The executor checks this too; checking here as well means
    # not paying for a judgement that would be refused anyway.
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

    trimmed = [{k: c.get(k) for k in FIELDS} for c in candidates]
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "prompt.txt").write_text(
        TASTE.read_text(encoding="utf-8") + "\n\n## Candidates\n\n" + json.dumps(trimmed, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"prompt written for {len(trimmed)} candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
