"""The judge's three scripts: batching, the request to the gateway, and the merge.

None of this needs a network. The request is made through an injected client,
and the SDK's typed errors through a stand-in with the same class tree. When the
real SDK is installed the same rules are run through it over a mock transport,
which is what proves the stand-in and the wire shape are not fiction.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

# The fixture suite is stdlib-only; the SDK is installed only in the judge pod.
HAVE_SDK = importlib.util.find_spec("anthropic") is not None

JUDGE_APP = "curatorjudge"
ID = "movie_id"
GROUP = "claude-opus"
TASTE = "RULES: delete what nobody would miss."


def _judge_dir() -> Path:
    """The judge app's scripts, when these tests run from a checkout.

    Walked rather than indexed off ``parents``: in-cluster the engine is mounted
    without its sibling app, and a fixed index would raise at import time.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / JUDGE_APP / "app" / "resources" / JUDGE_APP
        if (candidate / "judge.py").is_file():
            return candidate
    return here.with_name("judge-scripts-not-mounted")


JUDGE_DIR = _judge_dir()
MOUNTED = (JUDGE_DIR / "judge.py").is_file()


def load(script: str, **paths: Path) -> Any:
    """Import one of the judge's scripts with its paths pointed at a temp directory."""
    spec: Any = importlib.util.spec_from_file_location(f"{script}_under_test", JUDGE_DIR / f"{script}.py")
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, value in paths.items():
        setattr(module, name, value)
    return module


class _APIError(Exception):
    """Stand-in for the SDK's base error."""

    def __init__(self, message: str = "boom", status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _APIStatusError(_APIError):
    """Stand-in: any non-2xx reply."""


class _APIConnectionError(_APIError):
    """Stand-in: no reply at all."""


# The class tree the SDK documents. RealSdkTests fails if the SDK's own tree differs.
TREE = {
    "APIError": (Exception,),
    "APIStatusError": ("APIError",),
    "APIConnectionError": ("APIError",),
    "APITimeoutError": ("APIConnectionError",),
    "BadRequestError": ("APIStatusError",),
    "AuthenticationError": ("APIStatusError",),
    "PermissionDeniedError": ("APIStatusError",),
    "NotFoundError": ("APIStatusError",),
    "RateLimitError": ("APIStatusError",),
    "InternalServerError": ("APIStatusError",),
}


def _stand_in_sdk() -> SimpleNamespace:
    """A namespace holding error classes shaped like the SDK's."""
    classes: dict[str, type] = {
        "APIError": _APIError,
        "APIStatusError": _APIStatusError,
        "APIConnectionError": _APIConnectionError,
    }
    for name, (parent,) in TREE.items():
        if name not in classes:
            classes[name] = type(name, (classes[str(parent)],), {})
    return SimpleNamespace(**classes)


SDK = _stand_in_sdk()


class _Messages:  # pylint: disable=too-few-public-methods
    """Replays scripted replies and records each request."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        """Return the next reply, or raise it."""
        self.calls.append(kwargs)
        nxt = self.replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def client(*replies) -> SimpleNamespace:
    """A client whose ``messages.create`` answers with ``replies`` in turn."""
    return SimpleNamespace(messages=_Messages(replies))


def verdict(ident: int, call: str = "delete", reason: str = "feed filler nobody played") -> dict:
    """One well-formed verdict."""
    return {ID: ident, "verdict": call, "reason": reason}


def reply(verdicts=None, text=None, model=GROUP, stop_reason="end_turn", tokens=(1000, 200), details=None):
    """A reply shaped like the SDK's ``Message``: a thinking block, then the JSON text."""
    body = text if text is not None else json.dumps({"verdicts": verdicts or []})
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        stop_details=details,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=body)],
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


@unittest.skipUnless(MOUNTED, "judge scripts are not mounted in-cluster")
class JudgeScriptTests(unittest.TestCase):
    """A work directory, a state directory and a plan, shared by the cases below."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.work = root / "work"
        self.state = root / "state"
        self.work.mkdir()
        self.state.mkdir()
        self.taste = root / "taste.md"
        self.taste.write_text(TASTE, encoding="utf-8")

    def write_plan(self, count: int = 5, **extra) -> None:
        """A fresh, unblocked plan holding ``count`` candidates, largest first."""
        candidates = [
            {
                ID: n,
                "title": f"Film {n}",
                "size_gb": 100 - n,
                "origin": "requested" if n == 1 else "unknown",
                "requested_by": "Sam" if n == 1 else None,
                "original_language": "Hindi" if n == 2 else "English",
                "tmdb_id": 9000 + n,
                "rating_keys": [n],
            }
            for n in range(1, count + 1)
        ]
        plan = {
            "run_id": "this-week",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "blocks": [],
            "candidates": candidates,
            **extra,
        }
        (self.state / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    def build(self, batch: str | None = "2") -> int:
        """Run the prompt step with ``CLEANUP_JUDGE_BATCH`` set to ``batch``."""
        module = load("build_prompt", WORK=self.work, PLAN=self.state / "plan.json")
        env = {} if batch is None else {"CLEANUP_JUDGE_BATCH": batch}
        with mock.patch.dict(os.environ, env):
            if batch is None:
                os.environ.pop("CLEANUP_JUDGE_BATCH", None)
            return module.main()

    def judge(self) -> Any:
        """The judge script, pointed at this work directory."""
        return load("judge", WORK=self.work, TASTE=self.taste)

    def read(self) -> tuple[int, dict, dict]:
        """Run the validator; returns its exit code, the recommendations and the judgement."""
        module = load(
            "read_verdicts",
            WORK=self.work,
            PLAN=self.state / "plan.json",
            OUT=self.state / "recommendations.json",
            RECORD=self.state / "judgement.json",
        )
        code = module.main()
        return (
            code,
            json.loads((self.state / "recommendations.json").read_text(encoding="utf-8")),
            json.loads((self.state / "judgement.json").read_text(encoding="utf-8")),
        )

    def manifest(self) -> dict:
        """The batch list the prompt step wrote."""
        return json.loads((self.work / "batches.json").read_text(encoding="utf-8"))

    def result(self, name: str) -> dict:
        """One batch's result file."""
        return json.loads((self.work / f"result-{name}.json").read_text(encoding="utf-8"))


class BatchingTests(JudgeScriptTests):
    """The prompt step: one candidates file per batch, in the plan's order."""

    def test_candidates_are_split_into_batches_in_plan_order(self):
        self.write_plan(5)
        self.assertEqual(self.build("2"), 0)
        self.assertEqual([b["ids"] for b in self.manifest()["batches"]], [[1, 2], [3, 4], [5]])
        self.assertEqual([b["name"] for b in self.manifest()["batches"]], ["001", "002", "003"])
        self.assertEqual(
            sorted(p.name for p in self.work.glob("candidates-*.json")), [f"candidates-00{n}.json" for n in (1, 2, 3)]
        )

    def test_each_batch_file_holds_only_its_own_candidates(self):
        self.write_plan(5)
        self.build("2")
        second = json.loads((self.work / "candidates-002.json").read_text(encoding="utf-8"))
        self.assertEqual([c[ID] for c in second], [3, 4])

    def test_the_judge_is_shown_origin_requester_and_language(self):
        self.write_plan(2)
        self.build("2")
        first, second = json.loads((self.work / "candidates-001.json").read_text(encoding="utf-8"))
        self.assertEqual((first["origin"], first["requested_by"]), ("requested", "Sam"))
        self.assertEqual(second["original_language"], "Hindi")

    def test_identifiers_the_judge_has_no_use_for_stay_out(self):
        self.write_plan(1)
        self.build("2")
        (only,) = json.loads((self.work / "candidates-001.json").read_text(encoding="utf-8"))
        self.assertNotIn("tmdb_id", only)
        self.assertNotIn("rating_keys", only)

    def test_the_batch_list_names_the_plan_and_the_id_field(self):
        self.write_plan(1)
        self.build("2")
        self.assertEqual(self.manifest()["plan_run_id"], "this-week")
        self.assertEqual(self.manifest()["id_field"], ID)

    def test_what_an_earlier_attempt_left_is_cleared(self):
        for name in ("candidates-009.json", "result-001.json", "judge-failed", "skip"):
            (self.work / name).write_text("stale", encoding="utf-8")
        self.write_plan(1)
        self.build("2")
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["batches.json", "candidates-001.json"])

    def test_no_candidates_leaves_a_skip_marker_and_no_batches(self):
        self.write_plan(0)
        self.assertEqual(self.build("2"), 0)
        self.assertEqual([p.name for p in self.work.iterdir()], ["skip"])

    def test_a_blocked_plan_is_not_judged(self):
        self.write_plan(3, blocks=["play history not usable"])
        self.build("2")
        self.assertIn("blocking issues", (self.work / "skip").read_text(encoding="utf-8"))

    def test_an_unusable_batch_size_skips_rather_than_raising(self):
        for bad in ("0", "-3", "fifty"):
            with self.subTest(batch=bad):
                self.write_plan(3)
                self.assertEqual(self.build(bad), 0)
                self.assertIn("CLEANUP_JUDGE_BATCH", (self.work / "skip").read_text(encoding="utf-8"))

    def test_the_default_batch_size(self):
        self.write_plan(60)
        self.build(None)
        self.assertEqual([len(b["ids"]) for b in self.manifest()["batches"]], [50, 10])


class RequestTests(JudgeScriptTests):
    """One request for one batch, and the rules its reply has to pass."""

    def ask(self, *replies, candidates: str = '[{"movie_id": 1}]'):
        """Judge one batch against scripted replies; returns the result and the fake client."""
        fake = client(*replies)
        return self.judge().judge_batch(fake, SDK, GROUP, TASTE, candidates, ID), fake

    def test_a_good_reply_yields_its_verdicts_and_its_token_counts(self):
        result, _ = self.ask(
            reply([verdict(1), verdict(2, "keep", "cult standing, still revived")], tokens=(1200, 340))
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual([v[ID] for v in result["verdicts"]], [1, 2])
        self.assertEqual(result["usage"], {"input_tokens": 1200, "output_tokens": 340})
        self.assertEqual((result["model"], result["stop_reason"]), (GROUP, "end_turn"))

    def test_the_request_is_the_rules_the_candidates_and_the_schema(self):
        _, fake = self.ask(reply([verdict(1)]), candidates='[{"movie_id": 1, "title": "Film 1"}]')
        (sent,) = fake.messages.calls
        self.assertEqual(sent["model"], GROUP)
        self.assertEqual(sent["system"], TASTE)
        self.assertEqual(sent["messages"], [{"role": "user", "content": '[{"movie_id": 1, "title": "Film 1"}]'}])
        fmt = sent["output_config"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        item = fmt["schema"]["properties"]["verdicts"]["items"]
        self.assertEqual(item["properties"][ID], {"type": "integer"})
        self.assertEqual(item["properties"]["verdict"]["enum"], ["delete", "keep", "review"])
        self.assertEqual(sorted(item["required"]), sorted([ID, "verdict", "reason"]))
        self.assertIs(item["additionalProperties"], False)
        self.assertIs(fmt["schema"]["additionalProperties"], False)

    def test_nothing_else_is_sent(self):
        """No sampling, thinking or effort settings, and no fallback: the gateway reads that field its own way."""
        _, fake = self.ask(reply([verdict(1)]))
        (sent,) = fake.messages.calls
        self.assertEqual(sorted(sent), ["max_tokens", "messages", "model", "output_config", "system"])
        self.assertEqual(list(sent["output_config"]), ["format"])

    def test_any_stop_reason_but_end_turn_fails_the_batch(self):
        for stop in ("max_tokens", "refusal", "tool_use", "pause_turn", "stop_sequence", None):
            with self.subTest(stop_reason=stop):
                result, _ = self.ask(reply([verdict(1)], stop_reason=stop))
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("verdicts", result)
                self.assertIn(repr(stop), result["why"])

    def test_a_refusal_names_its_category(self):
        details = SimpleNamespace(category="cyber", explanation="declined")
        result, _ = self.ask(reply([verdict(1)], stop_reason="refusal", details=details))
        self.assertIn("category cyber", result["why"])

    def test_a_reply_from_another_model_fails_the_batch(self):
        """The gateway can fall back to a different model; a deletion verdict must not come from one."""
        for other in ("claude", "claude-opus-5-5", "local-fallback", None):
            with self.subTest(model=other):
                result, _ = self.ask(reply([verdict(1)], model=other))
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("verdicts", result)
                self.assertIn(f"not {GROUP!r}", result["why"])

    def test_a_wrong_model_reply_still_counts_its_tokens(self):
        result, _ = self.ask(reply([verdict(1)], model="other", tokens=(900, 50)))
        self.assertEqual(result["usage"], {"input_tokens": 900, "output_tokens": 50})

    def test_each_sdk_error_fails_the_batch_with_its_own_reason(self):
        cases = (
            (SDK.AuthenticationError("bad key", 401), "rejected the key"),
            (SDK.PermissionDeniedError("key not allowed", 403), "may not use model group"),
            (SDK.NotFoundError("no such model", 404), "has no model group"),
            (SDK.BadRequestError("bad schema", 400), "request was rejected"),
            (SDK.RateLimitError("slow down", 429), "rate limited"),
            (SDK.InternalServerError("overloaded", 529), "HTTP 529"),
            (SDK.APIStatusError("teapot", 418), "HTTP 418"),
            (SDK.APITimeoutError("timeout"), "timed out"),
            (SDK.APIConnectionError("refused"), "could not reach the gateway"),
            (SDK.APIError("bad body"), "could not use the reply"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                result, _ = self.ask(error)
                self.assertEqual(result["status"], "failed")
                self.assertIn(expected, result["why"])
                self.assertNotIn("verdicts", result)

    def test_json_that_does_not_match_the_schema_fails_the_batch(self):
        bad = {
            "not JSON at all": "I would delete the first one.",
            "a bare array": json.dumps([verdict(1)]),
            "an extra top-level key": json.dumps({"verdicts": [verdict(1)], "note": "x"}),
            "verdicts not a list": json.dumps({"verdicts": {"1": "delete"}}),
            "an entry that is not an object": json.dumps({"verdicts": ["delete"]}),
            "a verdict outside the three": json.dumps({"verdicts": [verdict(1, "obliterate")]}),
            "a missing reason": json.dumps({"verdicts": [{ID: 1, "verdict": "delete"}]}),
            "an extra key on a verdict": json.dumps({"verdicts": [{**verdict(1), "confidence": 0.9}]}),
            "an id that is a string": json.dumps({"verdicts": [{**verdict(1), ID: "1"}]}),
            "an id that is a boolean": json.dumps({"verdicts": [{**verdict(1), ID: True}]}),
            "a reason that is not text": json.dumps({"verdicts": [{**verdict(1), "reason": None}]}),
            "the other engine's id field": json.dumps(
                {"verdicts": [{"other_id": 1, "verdict": "keep", "reason": "x"}]}
            ),
        }
        for label, text in bad.items():
            with self.subTest(reply=label):
                result, _ = self.ask(reply(text=text))
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("verdicts", result)

    def test_a_reply_with_no_text_block_fails_the_batch(self):
        empty = reply([verdict(1)])
        empty.content = [SimpleNamespace(type="thinking", thinking="")]
        result, _ = self.ask(empty)
        self.assertIn("no text block", result["why"])

    def test_an_empty_verdict_list_is_valid_json_and_left_to_the_engine(self):
        """The engine counts the batch's titles as unjudged; that is not this step's call."""
        result, _ = self.ask(reply([]))
        self.assertEqual((result["status"], result["verdicts"]), ("ok", []))


class RunTests(JudgeScriptTests):
    """Every batch in turn: one failing does not stop or discard the others."""

    def setUp(self):
        super().setUp()
        self.write_plan(5)
        self.build("2")

    def run_judge(self, *replies, clock=None):
        """Judge all three batches against scripted replies."""
        fake = client(*replies)
        module = self.judge()
        kwargs = {"clock": clock} if clock else {}
        return module.judge_all(fake, SDK, GROUP, self.manifest(), **kwargs), fake

    def good(self, *ids: int):
        """A good reply giving a delete verdict for each id."""
        return reply([verdict(i) for i in ids], tokens=(1000, 100))

    def test_one_request_and_one_result_file_per_batch(self):
        _, fake = self.run_judge(self.good(1, 2), self.good(3, 4), self.good(5))
        self.assertEqual(len(fake.messages.calls), 3)
        self.assertEqual(
            sorted(p.name for p in self.work.glob("result-*.json")), [f"result-00{n}.json" for n in (1, 2, 3)]
        )
        self.assertEqual(json.loads(fake.messages.calls[1]["messages"][0]["content"])[0][ID], 3)

    def test_every_result_names_the_plan_and_its_batch(self):
        self.run_judge(self.good(1, 2), self.good(3, 4), self.good(5))
        for name in ("001", "002", "003"):
            self.assertEqual((self.result(name)["plan_run_id"], self.result(name)["batch"]), ("this-week", name))

    def test_a_mixed_run_keeps_the_batches_that_answered(self):
        results, _ = self.run_judge(self.good(1, 2), SDK.RateLimitError("slow down", 429), self.good(5))
        self.assertEqual([r["status"] for r in results], ["ok", "failed", "ok"])
        code, recommendations, judgement = self.read()
        self.assertEqual(code, 1)
        self.assertEqual(judgement["status"], "partial")
        self.assertEqual([r[ID] for r in recommendations["recommendations"]], [1, 2, 5])
        self.assertEqual(recommendations["plan_run_id"], "this-week")
        self.assertEqual(judgement["batches"]["asked"], 3)
        self.assertEqual(judgement["batches"]["judged"], 2)
        (failed,) = judgement["batches"]["failed"]
        self.assertEqual((failed["batch"], failed["candidates"]), ("002", 2))
        self.assertIn("rate limited", failed["why"])
        self.assertIn("2 of 3 batches judged", judgement["detail"])

    def test_a_reply_that_breaks_a_rule_fails_only_its_own_batch(self):
        self.run_judge(
            self.good(1, 2), reply([verdict(3)], model="local-fallback"), reply([verdict(5)], stop_reason="max_tokens")
        )
        code, recommendations, judgement = self.read()
        self.assertEqual(code, 1)
        self.assertEqual([r[ID] for r in recommendations["recommendations"]], [1, 2])
        self.assertEqual([f["batch"] for f in judgement["batches"]["failed"]], ["002", "003"])

    def test_an_unforeseen_error_in_one_batch_does_not_stop_the_next(self):
        results, fake = self.run_judge(ValueError("something nobody planned for"), self.good(3, 4), self.good(5))
        self.assertEqual([r["status"] for r in results], ["failed", "ok", "ok"])
        self.assertIn("ValueError", results[0]["why"])
        self.assertEqual(len(fake.messages.calls), 3)

    def test_token_totals_cover_every_batch_that_got_a_reply(self):
        self.run_judge(self.good(1, 2), reply([verdict(3)], model="other", tokens=(700, 30)), self.good(5))
        _, _, judgement = self.read()
        self.assertEqual(judgement["usage"], {"input_tokens": 2700, "output_tokens": 230})

    def test_batches_past_the_time_budget_are_not_attempted(self):
        ticks = iter([0, 0, 10, 10, 20, 9999, 9999, 9999, 9999])
        results, fake = self.run_judge(self.good(1, 2), self.good(3, 4), clock=lambda: next(ticks))
        self.assertEqual([r["status"] for r in results], ["ok", "ok", "failed"])
        self.assertIn("not attempted", results[2]["why"])
        self.assertEqual(len(fake.messages.calls), 2)

    def test_every_batch_judged_is_one_merged_answer(self):
        self.run_judge(self.good(1, 2), self.good(3, 4), self.good(5))
        code, recommendations, judgement = self.read()
        self.assertEqual(code, 0)
        self.assertEqual(judgement["status"], "judged")
        self.assertEqual(len(recommendations["recommendations"]), 5)
        self.assertEqual(judgement["verdicts"], {"delete": 5})
        self.assertIn("across 3 batches", judgement["detail"])

    def test_every_batch_failing_judges_nothing(self):
        self.run_judge(*(SDK.APIConnectionError("refused") for _ in range(3)))
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "failed"))
        self.assertEqual(recommendations["recommendations"], [])


class MergeTests(JudgeScriptTests):
    """The validator trusts nothing the judge step left behind."""

    def setUp(self):
        super().setUp()
        self.write_plan(4)
        self.build("2")

    def leave(self, name: str, **fields) -> None:
        """Write a result file as the judge would, with ``fields`` laid over a good one."""
        base = {"plan_run_id": "this-week", "batch": name, "status": "ok", "why": "", "verdicts": [], "usage": {}}
        (self.work / f"result-{name}.json").write_text(json.dumps({**base, **fields}), encoding="utf-8")

    def test_a_batch_with_no_result_file_is_a_failed_batch(self):
        self.leave("001", verdicts=[verdict(1), verdict(2)])
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "partial"))
        self.assertEqual(len(recommendations["recommendations"]), 2)
        self.assertIn("no judge result", judgement["batches"]["failed"][0]["why"])

    def test_a_result_left_by_another_plan_is_not_read(self):
        """Last week's file under this week's name must not become this week's answer."""
        self.leave("001", verdicts=[verdict(1)])
        self.leave("002", plan_run_id="last-week", verdicts=[verdict(3), verdict(4)])
        code, recommendations, judgement = self.read()
        self.assertEqual(code, 1)
        self.assertEqual([r[ID] for r in recommendations["recommendations"]], [1])
        self.assertIn("last-week", judgement["batches"]["failed"][0]["why"])

    def test_a_result_filed_under_another_batchs_name_is_not_read(self):
        self.leave("001", verdicts=[verdict(1)])
        self.leave("002", batch="001", verdicts=[verdict(3)])
        code, recommendations, _ = self.read()
        self.assertEqual(code, 1)
        self.assertEqual([r[ID] for r in recommendations["recommendations"]], [1])

    def test_a_batch_list_left_by_another_plan_refuses_everything(self):
        self.leave("001", verdicts=[verdict(1)])
        manifest = self.manifest()
        manifest["plan_run_id"] = "last-week"
        (self.work / "batches.json").write_text(json.dumps(manifest), encoding="utf-8")
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "failed"))
        self.assertEqual(recommendations["recommendations"], [])
        self.assertIn("last-week", judgement["detail"])

    def test_malformed_results_are_reported_as_malformed(self):
        (self.work / "result-001.json").write_text("{ truncated", encoding="utf-8")
        self.leave("002", verdicts="delete everything")
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "malformed"))
        self.assertEqual(recommendations["recommendations"], [])

    def test_one_malformed_result_does_not_discard_the_others(self):
        self.leave("001", verdicts=[verdict(1), verdict(2)])
        self.leave("002", verdicts=[verdict(3), "delete"])
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "partial"))
        self.assertEqual([r[ID] for r in recommendations["recommendations"]], [1, 2])
        self.assertIn("not objects", judgement["batches"]["failed"][0]["why"])

    def test_a_verdict_for_another_batchs_title_is_dropped(self):
        """The judge never saw that title's record; its rightful batch failed, so it has no verdict."""
        self.leave("001", verdicts=[verdict(1), verdict(2), verdict(3)])
        self.leave("002", status="failed", why="timed out")
        code, recommendations, judgement = self.read()
        self.assertEqual(code, 1)
        self.assertEqual([r[ID] for r in recommendations["recommendations"]], [1, 2])
        self.assertEqual(judgement["misfiled"], 1)

    def test_a_misfiled_verdict_alone_still_fails_the_run(self):
        self.leave("001", verdicts=[verdict(1), verdict(2), verdict(4, "keep", "a keep it was never asked for")])
        self.leave("002", verdicts=[verdict(3), verdict(4)])
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "partial"))
        self.assertEqual([(r[ID], r["verdict"]) for r in recommendations["recommendations"]][-1], (4, "delete"))
        self.assertEqual(len(recommendations["recommendations"]), 4)

    def test_a_verdict_for_a_title_in_no_batch_is_left_for_the_engine_to_reject(self):
        self.leave("001", verdicts=[verdict(1), verdict(2), verdict(999)])
        self.leave("002", verdicts=[verdict(3), verdict(4)])
        code, recommendations, _ = self.read()
        self.assertEqual(code, 0)
        self.assertIn(999, [r[ID] for r in recommendations["recommendations"]])

    def test_a_judge_step_that_stopped_early_keeps_what_it_finished(self):
        self.leave("001", verdicts=[verdict(1), verdict(2)])
        (self.work / "judge-failed").write_text("the judge step stopped early: OSError", encoding="utf-8")
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "partial"))
        self.assertEqual(len(recommendations["recommendations"]), 2)
        self.assertIn("stopped early", judgement["detail"])

    def test_an_install_failure_judges_nothing_and_says_why(self):
        (self.work / "judge-failed").write_text("could not install the SDK", encoding="utf-8")
        code, recommendations, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "failed"))
        self.assertEqual(recommendations["recommendations"], [])
        self.assertIn("could not install the SDK", judgement["detail"])

    def test_token_counts_that_are_not_numbers_are_not_summed(self):
        self.leave("001", verdicts=[verdict(1), verdict(2)], usage={"input_tokens": "lots", "output_tokens": 40})
        self.leave("002", verdicts=[verdict(3), verdict(4)], usage={"input_tokens": 500, "output_tokens": True})
        _, _, judgement = self.read()
        self.assertEqual(judgement["usage"], {"input_tokens": 500, "output_tokens": 40})


class StepNeverFailsTests(JudgeScriptTests):
    """The judge step always exits 0 and leaves the reason for the app step."""

    def test_a_skip_marker_means_nothing_is_asked(self):
        (self.work / "skip").write_text("no candidates", encoding="utf-8")
        self.assertEqual(self.judge().main(), 0)
        self.assertEqual([p.name for p in self.work.iterdir()], ["skip"])

    def test_a_missing_batch_list_leaves_a_marker_not_an_exit_code(self):
        self.assertEqual(self.judge().main(), 0)
        self.assertIn("stopped early", (self.work / "judge-failed").read_text(encoding="utf-8"))

    def test_an_unset_model_leaves_a_marker(self):
        self.write_plan(2)
        self.build("2")
        with mock.patch.dict(os.environ, {"JUDGE_MODEL": ""}):
            self.assertEqual(self.judge().main(), 0)
        self.assertIn("JUDGE_MODEL", (self.work / "judge-failed").read_text(encoding="utf-8"))

    @unittest.skipIf(HAVE_SDK, "the SDK is installed here")
    def test_a_missing_sdk_leaves_a_marker(self):
        self.write_plan(2)
        self.build("2")
        with mock.patch.dict(os.environ, {"JUDGE_MODEL": GROUP}):
            self.assertEqual(self.judge().main(), 0)
        self.assertIn("anthropic", (self.work / "judge-failed").read_text(encoding="utf-8"))
        code, _, judgement = self.read()
        self.assertEqual((code, judgement["status"]), (1, "failed"))


@unittest.skipUnless(MOUNTED and HAVE_SDK, "needs the judge scripts and the anthropic SDK")
class RealSdkTests(JudgeScriptTests):
    """The same rules through the real SDK, over a transport that never leaves the process."""

    def setUp(self):
        super().setUp()
        self.sdk: Any = importlib.import_module("anthropic")
        self.http: Any = importlib.import_module("httpx2")

    BODY = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": GROUP,
        "content": [{"type": "text", "text": json.dumps({"verdicts": [verdict(1)]})}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 321, "output_tokens": 45},
    }

    def ask(self, status: int = 200, body: dict | None = None, error: Exception | None = None):
        """Judge one batch through the SDK; returns the result and the requests it sent."""
        seen: list = []

        def handler(request):
            seen.append(request)
            if error:
                raise error
            return self.http.Response(status, json=self.BODY if body is None else body)

        env = {"ANTHROPIC_BASE_URL": "http://gateway.test:4000", "ANTHROPIC_AUTH_TOKEN": "test-key"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            sdk_client = self.sdk.Anthropic(
                http_client=self.sdk.DefaultHttpxClient(transport=self.http.MockTransport(handler)), max_retries=0
            )
        return self.judge().judge_batch(sdk_client, self.sdk, GROUP, TASTE, '[{"movie_id": 1}]', ID), seen

    def test_the_stand_in_error_tree_is_the_sdks(self):
        for name, (parent,) in TREE.items():
            if isinstance(parent, str):
                self.assertTrue(issubclass(getattr(self.sdk, name), getattr(self.sdk, parent)), name)

    def test_a_good_reply_over_the_wire(self):
        result, (request,) = self.ask()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["verdicts"], [verdict(1)])
        self.assertEqual(result["usage"], {"input_tokens": 321, "output_tokens": 45})
        self.assertEqual(str(request.url), "http://gateway.test:4000/v1/messages")
        self.assertEqual(request.headers["authorization"], "Bearer test-key")
        sent = json.loads(request.content)
        self.assertEqual(sorted(sent), ["max_tokens", "messages", "model", "output_config", "system"])
        self.assertEqual(sent["system"], TASTE)
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")

    def test_http_errors_are_the_typed_errors_the_chain_expects(self):
        cases = ((401, "rejected the key"), (403, "may not use model group"), (404, "has no model group"))
        cases += ((400, "request was rejected"), (429, "rate limited"), (500, "HTTP 500"), (529, "HTTP 529"))
        for status, expected in cases:
            with self.subTest(status=status):
                body = {"type": "error", "error": {"type": "some_error", "message": "refused by the gateway"}}
                result, _ = self.ask(status, body)
                self.assertEqual(result["status"], "failed")
                self.assertIn(expected, result["why"])

    def test_a_connection_failure_and_a_timeout_are_told_apart(self):
        result, _ = self.ask(error=self.http.ConnectError("refused"))
        self.assertIn("could not reach the gateway", result["why"])
        result, _ = self.ask(error=self.http.ReadTimeout("slow"))
        self.assertIn("timed out", result["why"])

    def test_a_reply_from_the_upstream_model_id_is_refused(self):
        result, _ = self.ask(body={**self.BODY, "model": "claude-opus-5-5"})
        self.assertEqual(result["status"], "failed")

    def test_a_cut_off_reply_is_refused(self):
        result, _ = self.ask(body={**self.BODY, "stop_reason": "max_tokens"})
        self.assertIn("'max_tokens'", result["why"])


if __name__ == "__main__":
    unittest.main()
