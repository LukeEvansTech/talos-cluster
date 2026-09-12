#!/usr/bin/env python3
"""Validate the judge's answer into a recommendations file.

The engine checks every id against the candidate set regardless, but a
malformed answer should fail here, where the message says so, rather than
looking like a run in which the model simply kept everything.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

WORK = Path(os.environ.get("WORK_DIR", "/work"))
OUT = Path(os.environ.get("RECOMMENDATIONS_PATH", "/state/cleanup-plan/recommendations.json"))

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def main() -> int:
    """Write recommendations.json, or an empty list when nothing was judged."""
    OUT.parent.mkdir(parents=True, exist_ok=True)

    if (WORK / "skip").is_file():
        print(f"nothing was judged: {(WORK / 'skip').read_text(encoding='utf-8').strip()}")
        OUT.write_text("[]", encoding="utf-8")
        return 0

    raw = WORK / "claude.json"
    if not raw.is_file():
        print(f"no judge output at {raw}", file=sys.stderr)
        return 1

    envelope = json.loads(raw.read_text(encoding="utf-8"))
    if envelope.get("is_error"):
        print(f"judge reported an error: {envelope.get('result') or envelope.get('subtype')}", file=sys.stderr)
        return 1

    print(
        f"judged in {envelope.get('duration_ms')}ms, "
        f"cost ${envelope.get('total_cost_usd')}, turns {envelope.get('num_turns')}"
    )

    body = FENCE.sub("", str(envelope.get("result", "")).strip())
    try:
        verdicts = json.loads(body)
    except json.JSONDecodeError as exc:
        print(f"judge did not return JSON: {exc}", file=sys.stderr)
        print(body[:400], file=sys.stderr)
        return 1

    if not isinstance(verdicts, list):
        print(f"expected a JSON array, got {type(verdicts).__name__}", file=sys.stderr)
        return 1

    OUT.write_text(json.dumps(verdicts, indent=2), encoding="utf-8")
    tally: dict[str, int] = {}
    for item in verdicts:
        verdict = str(item.get("verdict", "?"))
        tally[verdict] = tally.get(verdict, 0) + 1
    print(f"wrote {len(verdicts)} recommendations to {OUT}: {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
