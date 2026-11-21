#!/usr/bin/env python3
"""Validate the judge's answers into a recommendations file, and record what happened.

The engine checks every id against the candidate set regardless, but a malformed
answer should fail here, where the message says so, rather than looking like a
run in which the model simply kept everything.

The judge is asked in batches and leaves one result file for each. They are
merged here, and a batch that failed or left nothing usable does not discard the
others: its titles carry no verdict this run, which the engine reports as
unjudged, and the run still exits non-zero. The judge step is the least trusted
in the pipeline, so nothing in a result file is taken on its word.

Two files come out, always. `recommendations.json` is what the engine reads, and
holds an empty list whenever nothing usable was judged -- an absent file would
stop the pipeline before it reported anything. `judgement.json` is the account of
why, which the digest repeats: a week in which the judge could not be reached
should not read as a week in which it had nothing to say.

Both live on a volume that outlives the run, so last week's copies are sitting
there until something overwrites them. Two things follow. The refusal is written
*first*, before any answer is parsed, so that a crash anywhere below leaves a run
that judged nothing rather than one that appears to have re-issued last week's
verdicts. And both files name the plan they belong to, so the engine can tell
this week's answer from a leftover even if this script never runs at all. The
batch list and every result file are held to the same rule: one that names
another plan is refused.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

WORK = Path(os.environ.get("WORK_DIR", "/work"))
PLAN = Path(os.environ.get("PLAN_PATH", "/state/cleanup-plan/plan.json"))
OUT = Path(os.environ.get("RECOMMENDATIONS_PATH", "/state/cleanup-plan/recommendations.json"))
RECORD = Path(os.environ.get("JUDGEMENT_PATH", "/state/cleanup-plan/judgement.json"))

USAGE_KEYS = ("input_tokens", "output_tokens")


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


def read_manifest(run_id: str | None) -> tuple[dict | None, str]:
    """The batch list the prompt step wrote for this plan, or why it cannot be used."""
    path = WORK / "batches.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None, f"no batch list at {path}; did the prompt step run?"
    except json.JSONDecodeError as exc:
        return None, f"batch list at {path} was not JSON: {exc}"
    if not isinstance(manifest, dict) or not isinstance(manifest.get("batches"), list) or not manifest["batches"]:
        return None, f"batch list at {path} names no batches"
    if run_id is None or manifest.get("plan_run_id") != run_id:
        return None, f"batch list was written for plan {manifest.get('plan_run_id')!r}, not {run_id!r}"
    return manifest, ""


def usage_of(result: dict) -> dict:
    """The token counts a result states, keeping only whole numbers."""
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {k: usage[k] for k in USAGE_KEYS if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool)}


def read_batch(name: str, run_id: str | None) -> dict:
    """One batch's verdicts, or the reason it has none.

    ``verdicts`` is None for a batch that cannot be used. ``malformed`` tells a
    result that is there and wrong from one the judge never produced.
    """

    def unusable(why: str, malformed: bool = False, usage: dict | None = None) -> dict:
        return {"name": name, "verdicts": None, "why": why, "malformed": malformed, "usage": usage or {}, "ms": None}

    path = WORK / f"result-{name}.json"
    if not path.is_file():
        return unusable(f"no judge result at {path}")

    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return unusable(f"judge result was not JSON: {exc}", True)

    if not isinstance(result, dict):
        return unusable(f"judge result was a JSON {type(result).__name__}, not an object", True)

    if result.get("plan_run_id") != run_id or result.get("batch") != name:
        found = f"plan {result.get('plan_run_id')!r} batch {result.get('batch')!r}"
        return unusable(f"judge result belongs to {found}, not this run's batch {name}", True)

    usage = usage_of(result)
    if result.get("status") != "ok":
        return unusable(str(result.get("why") or "the judge recorded a failure without a reason"), False, usage)

    verdicts = result.get("verdicts")
    if not isinstance(verdicts, list):
        return unusable(f"expected a list of verdicts, got {type(verdicts).__name__}", True, usage)

    bad = [v for v in verdicts if not isinstance(v, dict)]
    if bad:
        return unusable(f"{len(bad)} entries are not objects, first {bad[0]!r}", True, usage)

    return {
        "name": name,
        "verdicts": verdicts,
        "why": "",
        "malformed": False,
        "usage": usage,
        "ms": result.get("duration_ms"),
        "model": result.get("model") if isinstance(result.get("model"), str) else None,
    }


def own_verdicts(result: dict, owner: dict, id_field: str) -> tuple[list, int]:
    """A batch's verdicts, less any that name a title another batch was given.

    The judge never saw that title's record, so the verdict is not one. Passed
    on, it would stand in for the rightful batch's answer, or for none at all if
    that batch failed. An id no batch holds is passed on for the engine to reject.
    """
    kept, misfiled = [], 0
    for verdict in result["verdicts"]:
        ident = verdict.get(id_field)
        holder = owner.get(ident) if isinstance(ident, int) and not isinstance(ident, bool) else None
        if holder is not None and holder != result["name"]:
            misfiled += 1
            continue
        kept.append(verdict)
    return kept, misfiled


def merge(manifest: dict, run_id: str | None, crash: str = "") -> tuple[str, str, int, list, dict]:
    """Every batch read and combined: status, detail, exit code, verdicts, record fields.

    ``crash`` is the judge step's own account of stopping early. The batches it
    finished before that still count.
    """
    batches = [b for b in manifest["batches"] if isinstance(b, dict)]
    id_field = str(manifest.get("id_field") or "")
    owner = {ident: str(b.get("name")) for b in batches for ident in b.get("ids") or []}
    results = [read_batch(str(b.get("name")), run_id) for b in batches]
    sizes = {str(b.get("name")): len(b.get("ids") or []) for b in batches}

    recommendations: list = []
    misfiled = 0
    for result in results:
        if result["verdicts"] is None:
            continue
        kept, dropped = own_verdicts(result, owner, id_field)
        recommendations += kept
        misfiled += dropped

    good = [r for r in results if r["verdicts"] is not None]
    failed = [r for r in results if r["verdicts"] is None]
    tally: dict[str, int] = {}
    for item in recommendations:
        verdict = str(item.get("verdict", "?"))
        tally[verdict] = tally.get(verdict, 0) + 1
    extra = {
        "verdicts": tally,
        # Tokens the judge used across every batch that got a reply, failed ones included.
        "usage": {key: sum(r["usage"].get(key, 0) for r in results) for key in USAGE_KEYS},
        "batches": {
            "asked": len(results),
            "judged": len(good),
            "failed": [{"batch": r["name"], "candidates": sizes.get(r["name"], 0), "why": r["why"]} for r in failed],
        },
        "misfiled": misfiled,
        # The gateway group that answered; judge.py refuses a reply from any other.
        "model": ", ".join(sorted({r["model"] for r in good if r.get("model")})) or None,
    }

    faults = [crash] if crash else []
    faults += [f"batch {r['name']} ({sizes.get(r['name'], 0)} titles): {r['why']}" for r in failed]
    if misfiled:
        faults.append(f"{misfiled} verdict(s) named a title from another batch and were dropped")

    if not faults:
        spent = sum(r["ms"] for r in good if isinstance(r["ms"], (int, float)) and not isinstance(r["ms"], bool))
        across = f" across {len(good)} batches" if len(good) > 1 else ""
        return "judged", f"{len(recommendations)} recommendations in {spent}ms{across}", 0, recommendations, extra
    if good:
        detail = f"{len(good)} of {len(results)} batches judged; " + "; ".join(faults)
        return "partial", detail, 1, recommendations, extra
    status = "malformed" if not crash and all(r["malformed"] for r in failed) else "failed"
    return status, "; ".join(faults), 1, [], extra


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

    # The judge step exits 0 whatever happens, so that this step runs and reports it.
    marker = WORK / "judge-failed"
    crash = (marker.read_text(encoding="utf-8").strip() or "the judge step failed") if marker.is_file() else ""

    manifest, refusal = read_manifest(run_id)
    if manifest is None:
        return finish("failed", "; ".join(part for part in (crash, refusal) if part), 1)

    status, detail, code, recommendations, extra = merge(manifest, run_id, crash)
    return finish(status, detail, code, recommendations, **extra)


if __name__ == "__main__":
    sys.exit(main())
