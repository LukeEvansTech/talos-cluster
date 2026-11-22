"""The review document as a contract: what a receiver can rely on.

The receiver is a separate application, so this suite is the only place a field
renamed here, or a signature computed differently, is caught before it reaches
one. ``review_example.json`` is the complete example document the README shows;
the builder has to produce it exactly from the inputs below.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curator import proposals, review

LIBRARY, ID_KEY = "film", "movie_id"
TESTS = Path(__file__).resolve().parent
EXAMPLE = TESTS / "review_example.json"
README = TESTS.parent / "README.md"
NOW = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)
PLAN_RUN = "run-20260912T080000-a1b2c3"
KEY = "0123456789abcdef0123456789abcdef"

ITEM_FIELDS = {
    "id": (int,),
    "title": (str,),
    "year": (int, float, type(None)),
    "size_gb": (int, float, type(None)),
    "imdb_score": (int, float, type(None)),
    "imdb_votes": (int, float, type(None)),
    "plays": (int, float, type(None)),
    "finished_by": (int, float, type(None)),
    "origin": (str,),
    "requested_by": (str,),
    "overview": (str,),
    "verdict": (str,),
    "by": (str,),
    "reason": (str,),
    "proposed_at": (str,),
    "due_at": (str,),
    "simulated": (bool,),
    "actions": (list,),
}
VERSION_1_FIELDS = (
    "version",
    "library",
    "generated_at",
    "run_id",
    "mode",
    "blocks",
    "cap",
    "next_run_at",
    "going",
    "later",
    "kept",
    "needs_you",
    "deleted",
)
LISTS = ("going", "later", "kept", "needs_you", "deleted")


def stored(item_id: int, title: str, size_gb: float, *, verdict="delete", by="judge", made=None, **facts) -> dict:
    """A proposal as the ledger holds it."""
    made = made or NOW - timedelta(days=7)
    reason = facts.pop("reason", "Straight-to-video sequel nobody has played.")
    proposal = proposals.judged(verdict, reason, made, 6) if by == "judge" else proposals.by_person(None, made)
    entry = {
        ID_KEY: item_id,
        "title": title,
        "year": 2014,
        "size_gb": size_gb,
        "imdb_score": 4.6,
        "imdb_votes": 2310,
        "play_count": 0,
        "distinct_completers": 0,
        "origin": "feed",
        "requested_by": None,
        "overview": "A synopsis from the metadata provider.",
        "fact_fingerprint": "abcdef0123456789",
        **facts,
        "proposal": proposal,
    }
    return proposals.record(entry, ID_KEY, "run-20260905T080000-d4e5f6")


def inputs() -> dict:
    """Everything the builder is given for the example: a dry run with one of each kind of title."""
    records = [
        stored(1, "Shark Lake 3", 40.2),
        stored(2, "A Kept Classic", 6.0, verdict="keep", reason="Cult standing; people would expect to find it."),
        stored(3, "Marked By Hand", 2.1, by="person"),
        stored(4, "Late Arrival", 8.5, made=NOW - timedelta(minutes=20), origin="requested", requested_by="sam"),
    ]
    plan = {
        "run_id": PLAN_RUN,
        "generated_at": (NOW - timedelta(minutes=30)).isoformat(),
        "counts": {
            "candidate": 2,
            "missing_file": 1,
            "not_due": 120,
            "protected": 5800,
            "proposed": 3,
            "review": 1,
            "spared": 40,
        },
        "judge_limit": 150,
        "candidates": [{ID_KEY: 10}, {ID_KEY: 11}],
        "judge_deferred": [],
        "proposed": [{ID_KEY: 1}, {ID_KEY: 2}, {ID_KEY: 3}],
        "lapsed_proposals": [{ID_KEY: 12}],
        "review": [{ID_KEY: 9, "title": "No Date", "size_gb": 1.5, "blockers": ["record has no usable added date"]}],
        "missing_file": [{ID_KEY: 13}],
    }
    result = {
        "plan_run_id": PLAN_RUN,
        "escalated": [{ID_KEY: 8, "title": "Could Go Either Way", "size_gb": 3.0, "reason": "Split audience."}],
        "deleted": [{ID_KEY: 7, "title": "Gone Already", "year": 2011, "size_gb": 3.2, "simulated": True}],
        "skipped": [{ID_KEY: 5, "why": "somebody is playing it"}],
        "failed": [],
        "uncertain": [],
        "deferred": [],
        "keep_tags_added": {"requested": 1, "applied": 0},
        "judge_keeps": {"due": 0, "kept": 0, "vetoed": [], "left_for_person": []},
    }
    judgement = {
        "plan_run_id": PLAN_RUN,
        "status": "judged",
        "detail": "2 recommendations in 41000ms",
        "verdicts": {"delete": 1, "keep": 1},
        "usage": {"input_tokens": 48210, "output_tokens": 3120},
        "batches": {"asked": 1, "judged": 1, "failed": []},
        "model": "claude-opus",
    }
    digest = review.digest_record(
        "Library cleanup: 2 would go on 19 Sep",
        "<b>DRY-RUN</b> · 5,967 films · 2 candidates\n<b>2 would go on 19 Sep</b> unless you keep them",
        "# Library cleanup\n\nRun run-20260912T080000-a1b2c3 · mode dry-run\n",
    )
    return {
        "plan": plan,
        "result": result,
        "records": {record[ID_KEY]: record for record in records},
        "judgement": judgement,
        "digest": digest,
    }


def build_example() -> dict:
    """The example document, from the builder."""
    given = inputs()
    return review.build(
        LIBRARY,
        ID_KEY,
        given["plan"],
        given["result"],
        given["records"],
        NOW,
        {"cap": 2, "mode": "dry-run", "blocks": []},
        judgement=given["judgement"],
        digest=given["digest"],
    )


class ExampleDocumentTests(unittest.TestCase):
    """The builder's output for a known week is the documented example, byte for byte."""

    def test_the_builder_produces_the_example(self):
        self.assertEqual(build_example(), json.loads(EXAMPLE.read_text(encoding="utf-8")))

    def test_the_example_covers_every_list(self):
        example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertTrue(all(example[name] for name in LISTS), [name for name in LISTS if not example[name]])

    @unittest.skipUnless(README.is_file() and LIBRARY == "film", "the contract is documented in the film README")
    def test_the_readme_shows_the_same_example(self):
        text = README.read_text(encoding="utf-8")
        block = re.search(r"<!-- review-example -->\s*```json\n(.*?)```", text, re.S)
        self.assertIsNotNone(block, "the README has no marked example block")
        self.assertEqual(json.loads(block.group(1)), json.loads(EXAMPLE.read_text(encoding="utf-8")))


class FieldTests(unittest.TestCase):
    """Every field has the type the README gives it, and version 1's fields are all still there."""

    def setUp(self):
        self.doc = build_example()

    def test_version_1_fields_are_kept(self):
        for name in VERSION_1_FIELDS:
            self.assertIn(name, self.doc)
        self.assertEqual(self.doc["version"], 2)

    def test_every_title_has_every_field_with_its_type(self):
        for name in LISTS:
            for item in self.doc[name]:
                self.assertEqual(set(item), set(ITEM_FIELDS), (name, item["id"]))
                for field, types in ITEM_FIELDS.items():
                    self.assertIsInstance(item[field], types, (name, field))

    def test_actions_offered_per_list(self):
        actions = {name: {tuple(item["actions"]) for item in self.doc[name]} for name in LISTS}
        self.assertEqual(actions["going"], {("keep",)})
        self.assertEqual(actions["later"], {("keep",)})
        # A judge's keep lasts a year: the page offers keeping it for good beside deleting it instead.
        self.assertEqual(actions["kept"], {("keep", "delete")})
        self.assertEqual(actions["needs_you"], {("keep",), ("keep", "delete")})
        self.assertEqual(actions["deleted"], {()})

    def test_the_summary_has_its_four_parts(self):
        summary = self.doc["summary"]
        self.assertEqual(set(summary), {"plan", "judge", "executor", "digest"})
        self.assertEqual(summary["plan"]["titles"], sum(summary["plan"]["counts"].values()))
        self.assertEqual(
            (summary["judge"]["batches"], summary["judge"]["failed_batches"], summary["judge"]["model"]),
            (1, 0, "claude-opus"),
        )
        self.assertEqual((summary["executor"]["would_delete"], summary["executor"]["deleted"]), (1, 0))
        self.assertEqual(set(summary["digest"]), {"title", "message", "text"})

    def test_a_week_with_no_plan_result_or_judgement_still_has_a_summary(self):
        doc = review.build(LIBRARY, ID_KEY, None, None, {}, NOW, {"cap": 30, "mode": "dry-run", "blocks": ["no plan"]})
        self.assertEqual((doc["summary"]["plan"], doc["summary"]["executor"], doc["summary"]["digest"]), (None,) * 3)
        self.assertEqual(doc["summary"]["judge"]["status"], "missing")
        self.assertIsNone(doc["summary"]["judge"]["batches"])

    def test_a_judgement_for_another_plan_is_not_summarised_as_this_ones(self):
        given = inputs()
        stale = {**given["judgement"], "plan_run_id": "last-week"}
        doc = review.build(LIBRARY, ID_KEY, given["plan"], None, {}, NOW, {"cap": 2}, judgement=stale)
        self.assertEqual(doc["summary"]["judge"]["status"], "failed")
        self.assertIsNone(doc["summary"]["judge"]["input_tokens"])

    def test_a_failed_batch_is_counted(self):
        given = inputs()
        failed = {**given["judgement"], "batches": {"asked": 3, "judged": 2, "failed": [{"batch": "002"}]}}
        doc = review.build(LIBRARY, ID_KEY, given["plan"], None, {}, NOW, {"cap": 2}, judgement=failed)
        self.assertEqual((doc["summary"]["judge"]["batches"], doc["summary"]["judge"]["failed_batches"]), (3, 1))

    def test_keep_tags_applied_counts_the_rating_bar_only(self):
        """A judge's keep writes no tag, so an old result's judge count is not added in."""
        given = inputs()
        result = {**given["result"], "keep_tags_added": {"applied": 2}, "judge_keeps": {"due": 3, "kept": 3}}
        doc = review.build(LIBRARY, ID_KEY, given["plan"], result, {}, NOW, {"cap": 2})
        self.assertEqual(doc["summary"]["executor"]["keep_tags_applied"], 2)
        legacy = {**result, "judge_keeps_tagged": {"applied": 3}}
        doc = review.build(LIBRARY, ID_KEY, given["plan"], legacy, {}, NOW, {"cap": 2})
        self.assertEqual(doc["summary"]["executor"]["keep_tags_applied"], 2)

    def test_kept_items_add_no_field_and_keep_the_version(self):
        doc = build_example()
        self.assertEqual(doc["version"], 2)
        self.assertEqual(set(doc["kept"][0]), set(doc["going"][0]))


class SignatureTests(unittest.TestCase):
    """A receiver can check a document with nothing but the README's description."""

    def test_the_signed_bytes_are_the_library_a_newline_and_the_body(self):
        body = review.encode(build_example())
        expected = hmac.new(KEY.encode("utf-8"), LIBRARY.encode("utf-8") + b"\n" + body, hashlib.sha256)
        self.assertEqual(review.sign(KEY, LIBRARY, body), "sha256=" + expected.hexdigest())

    def test_the_body_is_compact_sorted_ascii_json(self):
        doc = {"b": 1, "a": ["é"]}
        self.assertEqual(review.encode(doc), b'{"a":["\\u00e9"],"b":1}')

    def test_the_body_parses_back_to_the_document(self):
        self.assertEqual(json.loads(review.encode(build_example())), build_example())


if __name__ == "__main__":
    unittest.main()
