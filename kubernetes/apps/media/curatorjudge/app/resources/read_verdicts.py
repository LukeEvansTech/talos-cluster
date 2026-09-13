#!/usr/bin/env python3
"""Validate the judge's answer into a recommendations file, and record what happened.

The engine checks every id against the candidate set regardless, but a malformed
answer should fail here, where the message says so, rather than looking like a
run in which the model simply kept everything.

Two files come out, always. `recommendations.json` is what the engine reads, and
holds an empty list whenever nothing usable was judged -- an absent file would
stop the pipeline before it reported anything. `judgement.json` is the account of
why, which the digest repeats: a week in which the judge could not be reached
should not read as a week in which it had nothing to say.

Both live on a volume that outlives the run, so last week's copies are sitting
there until something overwrites them. Two things follow. The refusal is written
*first*, before the answer is parsed, so that a crash anywhere below leaves a run
that judged nothing rather than one that appears to have re-issued last week's
verdicts. And both files name the plan they belong to, so the engine can tell
this week's answer from a leftover even if this script never runs at all.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

WORK = Path(os.environ.get("WORK_DIR", "/work"))
PLAN = Path(os.environ.get("PLAN_PATH", "/state/cleanup-plan/plan.json"))
OUT = Path(os.environ.get("RECOMMENDATIONS_PATH", "/state/cleanup-plan/recommendations.json"))
RECORD = Path(os.environ.get("JUDGEMENT_PATH", "/state/cleanup-plan/judgement.json"))

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def plan_run_id() -> str | None:
    """The run the plan on disk belongs to, or None if it cannot be read.

    None is a usable answer: it will not match any plan, so the engine refuses
    rather than acting on verdicts nobody can tie to a candidate list.
    """
    try:
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return plan.get("run_id") if isinstance(plan, dict) else None


def write_state(run_id: str | None, status: str, detail: str, recommendations: list | None = None, **extra) -> None:
    """Overwrite both files so they describe `status` and nothing earlier."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"plan_run_id": run_id, "recommendations": recommendations or []}, indent=2),
        encoding="utf-8",
    )
    RECORD.write_text(
        json.dumps({"status": status, "detail": detail, "plan_run_id": run_id, **extra}, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    """Write recommendations.json and judgement.json."""
    run_id = plan_run_id()

    def finish(status: str, detail: str, code: int, recommendations: list | None = None, **extra) -> int:
        write_state(run_id, status, detail, recommendations, **extra)
        print(f"{status}: {detail}", file=sys.stderr if code else sys.stdout)
        return code

    # The seal. Everything below can raise; this is what is left behind if it
    # does, and it deletes nothing.
    write_state(run_id, "failed", "read_verdicts did not finish")

    if (WORK / "skip").is_file():
        return finish("not asked", (WORK / "skip").read_text(encoding="utf-8").strip(), 0)

    if (WORK / "judge-failed").is_file():
        # The judge container records its failure here and exits 0 on purpose, so
        # that a judgement this pipeline could not obtain still gets reported
        # rather than killing the pod before the digest is sent.
        return finish("failed", (WORK / "judge-failed").read_text(encoding="utf-8").strip(), 1)

    raw = WORK / "claude.json"
    if not raw.is_file():
        return finish("failed", f"no judge output at {raw}", 1)

    try:
        envelope = json.loads(raw.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return finish("failed", f"judge envelope was not JSON: {exc}", 1)

    if not isinstance(envelope, dict):
        return finish("failed", f"judge envelope was a JSON {type(envelope).__name__}, not an object", 1)

    if envelope.get("is_error"):
        return finish("failed", str(envelope.get("result") or envelope.get("subtype")), 1)

    cost = envelope.get("total_cost_usd")
    body = FENCE.sub("", str(envelope.get("result", "")).strip())
    try:
        verdicts = json.loads(body)
    except json.JSONDecodeError as exc:
        print(body[:400], file=sys.stderr)
        return finish("malformed", f"judge did not return JSON: {exc}", 1, cost_usd=cost)

    if not isinstance(verdicts, list):
        return finish("malformed", f"expected a JSON array, got {type(verdicts).__name__}", 1, cost_usd=cost)

    bad = [v for v in verdicts if not isinstance(v, dict)]
    if bad:
        return finish("malformed", f"{len(bad)} entries are not objects, first {bad[0]!r}", 1, cost_usd=cost)

    tally: dict[str, int] = {}
    for item in verdicts:
        verdict = str(item.get("verdict", "?"))
        tally[verdict] = tally.get(verdict, 0) + 1
    return finish(
        "judged",
        f"{len(verdicts)} recommendations in {envelope.get('duration_ms')}ms",
        0,
        recommendations=verdicts,
        verdicts=tally,
        cost_usd=cost,
    )


if __name__ == "__main__":
    sys.exit(main())
