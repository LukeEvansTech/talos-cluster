#!/usr/bin/env python3
"""Ask the judge about each batch of candidates: one API request per batch.

The only step that talks to a model, and the least trusted for it. It holds no
credential but the gateway key, mounts no state volume, and never exits
non-zero: what happened to each batch is written to /work, where the app step
reads it and reports. One batch failing does not stop the next.

Read from /work, as build_prompt.py left it:

- ``batches.json``: the plan's run id, the id field, and each batch's name and ids;
- ``candidates-NNN.json``: one batch's candidate records, sent as the user message.

Written to /work, one per batch, as ``result-NNN.json``:

    {"plan_run_id": ..., "batch": "NNN", "status": "ok" | "failed", "why": ...,
    "verdicts": [...], "model": ..., "stop_reason": ..., "duration_ms": ...,
    "usage": {"input_tokens": ..., "output_tokens": ...}}

``verdicts`` is present only when ``status`` is ``ok``. Every file names the plan,
so the validator can refuse one that belongs to another run.

The request goes to the cluster's LiteLLM gateway, which the zero-argument
client finds through ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_AUTH_TOKEN``.
``JUDGE_MODEL`` is a gateway model group, not a model id, and the gateway
answers with that group's name in ``model``: a reply naming anything else was
served by something this did not ask for, and is refused.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

WORK = Path(os.environ.get("WORK_DIR", "/work"))
TASTE = Path(os.environ.get("TASTE_PATH", "/prompt/taste.md"))

VERDICTS = ("delete", "keep", "review")

# The most a non-streaming request can ask for inside the SDK's ten-minute timeout. A batch
# that runs out fails by rule rather than being cut short; a smaller CLEANUP_JUDGE_BATCH fixes it.
MAX_TOKENS = 16000

# No batch starts after this many seconds. One request can take thirty minutes at worst (the
# SDK's ten-minute timeout, three attempts), so the step ends inside the Job's one-hour deadline.
START_BUDGET_SECONDS = 1500


def verdict_schema(id_field: str) -> dict:
    """The structured-output schema: one verdict object per candidate."""
    return {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        id_field: {"type": "integer"},
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "reason": {"type": "string"},
                    },
                    "required": [id_field, "verdict", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["verdicts"],
        "additionalProperties": False,
    }


def parse_verdicts(text: str, id_field: str) -> tuple[list | None, str]:
    """The verdicts in a reply, or why it does not match the schema.

    Checked here although the API constrains the output: the reply crosses a
    gateway, and a refusal or a cut-off can leave it short of the schema.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"reply was not JSON: {exc}"
    if not isinstance(payload, dict) or set(payload) != {"verdicts"} or not isinstance(payload["verdicts"], list):
        return None, 'reply was not an object holding only a "verdicts" array'
    for index, item in enumerate(payload["verdicts"]):
        if not isinstance(item, dict) or set(item) != {id_field, "verdict", "reason"}:
            return None, f"verdict {index} does not have exactly {id_field}, verdict and reason"
        ident = item[id_field]
        if not isinstance(ident, int) or isinstance(ident, bool):
            return None, f"verdict {index} has a {id_field} that is not an integer"
        if item["verdict"] not in VERDICTS:
            return None, f"verdict {index} is {item['verdict']!r}, not one of {', '.join(VERDICTS)}"
        if not isinstance(item["reason"], str):
            return None, f"verdict {index} has a reason that is not a string"
    return payload["verdicts"], ""


def usage_of(response) -> dict:
    """Input and output token counts from a reply, as far as it states them."""
    usage = getattr(response, "usage", None)
    counts = {}
    for key in ("input_tokens", "output_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int) and not isinstance(value, bool):
            counts[key] = value
    return counts


def read_reply(response, model: str, id_field: str) -> dict:
    """Apply the rules a reply has to pass before any of its verdicts count."""
    record = {"model": response.model, "stop_reason": response.stop_reason, "usage": usage_of(response)}
    if record["model"] != model:
        # The gateway can serve a group from another model; a deletion verdict must not come from one.
        return {**record, "status": "failed", "why": f"reply came from model {record['model']!r}, not {model!r}"}
    if record["stop_reason"] != "end_turn":
        why = f"stop_reason was {record['stop_reason']!r}, not 'end_turn'"
        details = getattr(response, "stop_details", None)
        if record["stop_reason"] == "refusal" and getattr(details, "category", None):
            why += f" (category {details.category})"
        return {**record, "status": "failed", "why": why}
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        return {**record, "status": "failed", "why": "reply held no text block"}
    verdicts, why = parse_verdicts(text, id_field)
    if verdicts is None:
        return {**record, "status": "failed", "why": why}
    return {**record, "status": "ok", "why": "", "verdicts": verdicts}


def judge_batch(client, sdk, model: str, system: str, candidates: str, id_field: str) -> dict:
    """One request for one batch, and what came of it.

    ``sdk`` is the ``anthropic`` module, passed in so the typed errors can be
    caught without importing it here. They are raised only after the SDK's own
    retries, so each one is final for this batch.
    """
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": candidates}],
            output_config={"format": {"type": "json_schema", "schema": verdict_schema(id_field)}},
        )
    except sdk.AuthenticationError as exc:
        why = f"the gateway rejected the key (HTTP 401): {exc.message}"
    except sdk.PermissionDeniedError as exc:
        why = f"the key may not use model group {model!r} (HTTP 403): {exc.message}"
    except sdk.NotFoundError as exc:
        why = f"the gateway has no model group {model!r} (HTTP 404): {exc.message}"
    except sdk.BadRequestError as exc:
        why = f"the request was rejected (HTTP 400): {exc.message}"
    except sdk.RateLimitError as exc:
        why = f"rate limited after the SDK's retries (HTTP 429): {exc.message}"
    except sdk.InternalServerError as exc:
        why = f"gateway or provider error after the SDK's retries (HTTP {exc.status_code}): {exc.message}"
    except sdk.APIStatusError as exc:
        why = f"HTTP {exc.status_code} from the gateway: {exc.message}"
    except sdk.APITimeoutError:
        why = "the request timed out on every attempt"
    except sdk.APIConnectionError as exc:
        why = f"could not reach the gateway: {exc}"
    except sdk.APIError as exc:
        why = f"the SDK could not use the reply: {exc}"
    else:
        return read_reply(response, model, id_field)
    return {"status": "failed", "why": why[:400], "usage": {}}


def judge_all(client, sdk, model: str, manifest: dict, clock=time.monotonic) -> list[dict]:
    """Judge every batch in turn, writing one result file each. Returns the results."""
    system = TASTE.read_text(encoding="utf-8")
    id_field = str(manifest.get("id_field") or "")
    started = clock()
    results = []
    for batch in manifest["batches"]:
        name = str(batch["name"])
        began = clock()
        if began - started > START_BUDGET_SECONDS:
            result = {"status": "failed", "why": "not attempted: the judge step's time budget was spent", "usage": {}}
        else:
            try:
                candidates = (WORK / f"candidates-{name}.json").read_text(encoding="utf-8")
                result = judge_batch(client, sdk, model, system, candidates, id_field)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                # Anything unforeseen is this batch's failure, recorded like any other, so the
                # batches after it still run and the step still hands over.
                result = {"status": "failed", "why": f"{type(exc).__name__}: {exc}"[:400], "usage": {}}
        result = {
            "plan_run_id": manifest.get("plan_run_id"),
            "batch": name,
            "duration_ms": int((clock() - began) * 1000),
            **result,
        }
        (WORK / f"result-{name}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        outcome = "judged" if result["status"] == "ok" else f"FAILED: {result['why']}"
        print(f"batch {name}: {outcome}", file=sys.stdout if result["status"] == "ok" else sys.stderr, flush=True)
        results.append(result)
    return results


def run() -> None:
    """Judge this run's batches, or leave the marker that says why not."""
    if (WORK / "skip").is_file():
        print(f"skipping: {(WORK / 'skip').read_text(encoding='utf-8').strip()}")
        return
    manifest = json.loads((WORK / "batches.json").read_text(encoding="utf-8"))
    model = os.environ.get("JUDGE_MODEL", "").strip()
    if not model:
        raise RuntimeError("JUDGE_MODEL is not set")

    import anthropic  # pylint: disable=import-outside-toplevel,import-error

    # Zero-argument on purpose: base URL and key come from the environment, and the
    # SDK's default retries and timeout stay as they are.
    judge_all(anthropic.Anthropic(), anthropic, model, manifest)


def main() -> int:
    """Run, and turn anything that escapes into a marker. Always returns 0."""
    try:
        run()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # The app step must still run and send the digest, so the reason is left for it.
        reason = f"the judge step stopped early: {type(exc).__name__}: {exc}"[:400]
        print(reason, file=sys.stderr)
        WORK.mkdir(parents=True, exist_ok=True)
        (WORK / "judge-failed").write_text(reason, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
