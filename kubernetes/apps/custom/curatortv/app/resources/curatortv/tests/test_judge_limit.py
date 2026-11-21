"""The whole library is in scope, so the judge and the deletion cap are both budgeted.

Candidates past the judge limit are deferred: never offered, so no verdict is
owed for them and they must not read as a judge that failed. The settings that
set these budgets are read before anything is fetched or written.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from curatortv import __main__ as cli
from curatortv import proposals
from curatortv.__main__ import build_plan_payload, to_show
from curatortv.ledger import Ledger
from curatortv.planner import PlanContext, assess
from curatortv.settings import env_int, scope_start

from .fixtures import (
    NOW,
    FakeS3,
    FakeSonarr,
    FakeTautulli,
    days_ago,
    make_availability,
    make_show,
    make_viewing,
    propose,
    series_record,
    unknown_provenance,
)

RUN: dict[str, Any] = {
    "snapshot": {},
    "blocks": [],
    "recycle_bin": "/recycle, 14 days",
    "history": {},
    "coverage_start": None,
    "unattributed": 0,
    "horizon": None,
    "keep_targets": [],
}


def context(**overrides) -> PlanContext:
    """A plan context with no scope boundary, as the manifests now run."""
    base: dict[str, Any] = {
        "now": NOW,
        "scope_start": None,
        "coverage_start": days_ago(5000),
        "history_ok": True,
    }
    return PlanContext(**{**base, **overrides})


def payload(sizes: dict[int, int], limit: int | None = None, ctx: PlanContext | None = None) -> dict:
    """The plan for a library of candidates, each ``series_id`` given its size in GB."""
    ctx = ctx or context()
    assessments = [
        assess(
            make_show(series_id=series_id, size_bytes=gb * 1_000_000_000, tags=frozenset(), added=days_ago(2000)),
            unknown_provenance(),
            make_availability(days=1500),
            make_viewing(),
            ctx,
        )
        for series_id, gb in sizes.items()
    ]
    run = RUN if limit is None else {**RUN, "judge_limit": limit}
    return build_plan_payload("run-plan", NOW, True, ctx, assessments, run)


class PlanSplitTests(unittest.TestCase):
    """The plan names the candidates the judge is asked about, and sets the rest aside."""

    def test_the_largest_candidates_are_offered_and_the_rest_deferred(self):
        plan = payload({1: 5, 2: 40, 3: 20, 4: 1}, limit=2)
        self.assertEqual([c["series_id"] for c in plan["candidates"]], [2, 3])
        self.assertEqual([c["series_id"] for c in plan["judge_deferred"]], [1, 4])
        self.assertEqual(plan["judge_limit"], 2)

    def test_the_counts_still_show_every_candidate(self):
        self.assertEqual(payload({1: 5, 2: 40, 3: 20}, limit=1)["counts"], {"candidate": 3})

    def test_candidates_are_handed_over_largest_first(self):
        plan = payload({1: 5, 2: 40, 3: 20})
        self.assertEqual([c["series_id"] for c in plan["candidates"]], [2, 3, 1])
        self.assertEqual(plan["judge_deferred"], [])

    def test_a_limit_of_zero_offers_nothing(self):
        plan = payload({1: 5, 2: 40}, limit=0)
        self.assertEqual(plan["candidates"], [])
        self.assertEqual(len(plan["judge_deferred"]), 2)

    def test_the_series_record_is_read_with_its_original_language(self):
        record = {**series_record(), "originalLanguage": {"id": 26, "name": "Hindi"}}
        self.assertEqual(to_show(record, {}).original_language, "Hindi")
        self.assertIsNone(to_show(series_record(), {}).original_language)

    def test_a_plan_with_no_boundary_says_so(self):
        self.assertIsNone(payload({1: 5})["scope_start"])

    def test_a_plan_with_a_boundary_records_it(self):
        boundary = datetime(2026, 3, 1, tzinfo=timezone.utc)
        self.assertEqual(payload({1: 5}, ctx=context(scope_start=boundary))["scope_start"], boundary.isoformat())

    def test_the_judge_record_carries_origin_requester_and_language(self):
        ctx = context()
        show = make_show(tags=frozenset(), added=days_ago(2000), original_language="Hindi")
        record = assess(show, unknown_provenance(), make_availability(days=1500), make_viewing(), ctx).to_json()
        self.assertEqual(
            (record["origin"], record["requested_by"], record["original_language"]), ("unknown", None, "Hindi")
        )


class SettingsTests(unittest.TestCase):
    """A budget that cannot be read stops the run; it is never quietly replaced."""

    def setting(self, name: str, value: str | None):
        """The environment with one variable set, or unset."""
        patcher = mock.patch.dict(os.environ, {} if value is None else {name: value})
        patcher.start()
        self.addCleanup(patcher.stop)
        if value is None:
            os.environ.pop(name, None)

    def test_an_unset_number_takes_the_default(self):
        self.setting("CLEANUP_MAX_DELETIONS", None)
        self.assertEqual(env_int("CLEANUP_MAX_DELETIONS", 10), 10)

    def test_a_set_number_overrides_the_default(self):
        self.setting("CLEANUP_MAX_DELETIONS", " 100 ")
        self.assertEqual(env_int("CLEANUP_MAX_DELETIONS", 30), 100)

    def test_zero_is_a_number(self):
        self.setting("CLEANUP_MAX_DELETIONS", "0")
        self.assertEqual(env_int("CLEANUP_MAX_DELETIONS", 30), 0)

    def test_a_number_that_cannot_be_read_stops_the_run(self):
        for bad in ("many", "30.5", "-1"):
            with self.subTest(value=bad):
                self.setting("CLEANUP_MAX_DELETIONS", bad)
                with self.assertRaises(SystemExit) as raised:
                    env_int("CLEANUP_MAX_DELETIONS", 30)
                self.assertIn("CLEANUP_MAX_DELETIONS", str(raised.exception))

    def test_no_boundary_is_the_default(self):
        self.setting("CLEANUP_SCOPE_START", None)
        self.assertIsNone(scope_start())
        self.setting("CLEANUP_SCOPE_START", "  ")
        self.assertIsNone(scope_start())

    def test_a_boundary_that_is_set_still_works(self):
        self.setting("CLEANUP_SCOPE_START", "2026-03-01")
        self.assertEqual(scope_start(), datetime(2026, 3, 1, tzinfo=timezone.utc))

    def test_a_boundary_that_cannot_be_read_stops_the_run(self):
        """It used to become "now", which put the whole library out of scope without a word."""
        self.setting("CLEANUP_SCOPE_START", "last spring")
        with self.assertRaises(SystemExit):
            scope_start()


class ExecuteTests(unittest.TestCase):  # pylint: disable=too-many-public-methods
    """Deferred candidates owe no verdict, a verdict waits out its window, and the cap comes from the environment."""

    CANDIDATE = {"origin": "unknown", "availability_source": "sonarr-history", "history_status": "ok"}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "judge_limit": 3,
            "candidates": [{"series_id": n, **self.CANDIDATE} for n in (1, 2, 3)],
            "judge_deferred": [{"series_id": n, **self.CANDIDATE} for n in (4, 5)],
            "proposed": [],
        }
        records = {
            n: series_record(n, tvdb_id=70000 + n, tmdb_id=1000 * n, imdb_id=f"tt000000{n}") for n in range(1, 9)
        }
        self.sonarr = FakeSonarr(records, {1: "keep", 5: "cleanup-eligible"})
        self.ledger = Ledger(FakeS3(), "run-execute", dry_run=True)
        sources = SimpleNamespace(
            sonarr=self.sonarr,
            tautulli=FakeTautulli(),
            seerr=None,
            ledger=self.ledger,
            run_id="run-execute",
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def due(self, item_id: int, by: str = "judge", age_days: float = 7.0, verdict: str = "delete") -> dict:
        """Put a proposal for this series on record and list it in the plan, as an earlier run would have."""
        entry = propose(self.ledger, {"series_id": item_id, **self.CANDIDATE}, verdict, by, age_days)
        self.plan["proposed"].append(entry)
        return entry

    def resize(self, item_id: int, size: int) -> None:
        """Change how much disk a live series takes."""
        self.sonarr.series_by_id[item_id]["statistics"]["sizeOnDisk"] = size

    def execute(self, verdicts: dict[int, str], **settings: str) -> tuple[int, dict]:
        """Run execute against ``{series_id: verdict}`` and return its exit code and result."""
        recommendations = [
            {"series_id": series_id, "verdict": verdict, "reason": "never played and no standing to name"}
            for series_id, verdict in verdicts.items()
        ]
        (self.root / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")
        path = self.root / "recs.json"
        path.write_text(json.dumps({"plan_run_id": "plan-1", "recommendations": recommendations}), encoding="utf-8")
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"), recommendations=str(path), out=str(self.root / "r.json"), read_only=True
        )
        with mock.patch.dict(os.environ, settings):
            for name in ("CLEANUP_MAX_DELETIONS", "CLEANUP_GRACE_DAYS"):
                if name not in settings:
                    os.environ.pop(name, None)
            code = cli.cmd_execute(args)
        return code, json.loads((self.root / "r.json").read_text(encoding="utf-8"))

    REVIEW_ALL = {1: "review", 2: "review", 3: "review"}

    def test_deferred_candidates_do_not_fail_the_run(self):
        """Every candidate that was offered has a verdict, so the week is a clean one."""
        code, result = self.execute({1: "delete", 2: "review", 3: "review"})
        self.assertEqual(code, 0)
        self.assertEqual(result["unjudged"], [])

    def test_an_offered_candidate_with_no_verdict_still_fails_it(self):
        code, result = self.execute({1: "delete", 2: "review"})
        self.assertEqual(code, 9)
        self.assertEqual(result["unjudged"], [3])

    def test_a_verdict_for_a_deferred_candidate_is_rejected_and_not_acted_on(self):
        code, result = self.execute({1: "review", 2: "review", 3: "review", 4: "delete"})
        self.assertEqual(code, 9)
        self.assertEqual(result["deleted"], [])
        self.assertIn("not in this run's candidate set", result["rejected_recommendations"][0]["why"])
        self.assertNotIn(4, self.ledger.read_proposals())

    def test_a_verdict_is_put_on_record_and_not_carried_out_in_the_run_that_gave_it(self):
        code, result = self.execute({1: "delete", 2: "keep", 3: "review"})
        self.assertEqual(code, 0)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(self.sonarr.tag_calls, [])
        self.assertEqual(result["proposals"]["recorded"], {"delete": 1, "keep": 1})
        stored = self.ledger.read_proposals()
        self.assertEqual({key: value["verdict"] for key, value in stored.items()}, {1: "delete", 2: "keep"})
        self.assertEqual(stored[1]["by"], "judge")

    def test_a_verdict_waits_six_days_by_default(self):
        self.execute({1: "delete", 2: "review", 3: "review"})
        stored = self.ledger.read_proposals()[1]
        waited = datetime.fromisoformat(stored["due_at"]) - datetime.fromisoformat(stored["proposed_at"])
        self.assertEqual(waited, timedelta(days=6))

    def test_the_wait_can_be_set_from_the_environment(self):
        _, result = self.execute({1: "delete", 2: "review", 3: "review"}, CLEANUP_GRACE_DAYS="2")
        stored = self.ledger.read_proposals()[1]
        waited = datetime.fromisoformat(stored["due_at"]) - datetime.fromisoformat(stored["proposed_at"])
        self.assertEqual(waited, timedelta(days=2))
        self.assertEqual(result["proposals"]["grace_days"], 2)

    def test_a_wait_of_nothing_still_does_not_act_in_the_run_that_judged(self):
        """Even due at once, a verdict is not in this run's plan, so it waits for the next run."""
        _, result = self.execute({1: "delete", 2: "review", 3: "review"}, CLEANUP_GRACE_DAYS="0")
        self.assertEqual(result["deleted"], [])
        self.assertIn(1, self.ledger.read_proposals())

    def test_a_review_is_recorded_as_a_decision_and_not_as_a_proposal(self):
        _, result = self.execute(self.REVIEW_ALL)
        self.assertEqual(self.ledger.read_proposals(), {})
        self.assertEqual(sorted(self.ledger.read_decisions()), [1, 2, 3])
        self.assertEqual(len(result["escalated"]), 3)

    def test_a_due_delete_is_carried_out(self):
        self.due(6)
        code, result = self.execute(self.REVIEW_ALL)
        self.assertEqual(code, 0)
        self.assertEqual([item["series_id"] for item in result["deleted"]], [6])
        self.assertEqual(result["proposals"]["due"], {"delete": 1, "keep": 0})

    def test_a_rehearsal_leaves_the_proposal_standing(self):
        """Nothing was deleted, so next week has to find it due again rather than judge it again."""
        self.due(6)
        _, result = self.execute(self.REVIEW_ALL)
        self.assertTrue(result["deleted"][0]["simulated"])
        self.assertEqual(self.sonarr.deleted, [])
        self.assertIn(6, self.ledger.read_proposals())

    def test_a_proposal_still_inside_its_wait_is_left_alone(self):
        self.due(6, age_days=1)
        _, result = self.execute(self.REVIEW_ALL)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["proposals"]["due"], {"delete": 0, "keep": 0})

    def test_a_due_delete_with_nothing_on_record_is_not_acted_on(self):
        """The plan is a file on a volume; a deletion needs the ledger to agree with it."""
        self.due(6)
        self.ledger.delete_proposal(6)
        code, result = self.execute(self.REVIEW_ALL)
        self.assertEqual(code, 0)
        self.assertEqual(result["deleted"], [])
        self.assertIn("no matching proposal", result["skipped"][0]["why"])

    def test_a_due_delete_whose_record_says_something_else_is_not_acted_on(self):
        entry = self.due(6)
        entry["proposal"]["due_at"] = "2020-01-01T00:00:00+00:00"
        _, result = self.execute(self.REVIEW_ALL)
        self.assertEqual(result["deleted"], [])
        self.assertIn("no matching proposal", result["skipped"][0]["why"])

    def test_a_lapsed_proposal_is_taken_off_the_record(self):
        propose(self.ledger, {"series_id": 6, **self.CANDIDATE})
        self.plan["lapsed_proposals"] = [{"series_id": 6, "why": "now protected: carries cleanup-keep"}]
        _, result = self.execute(self.REVIEW_ALL)
        self.assertNotIn(6, self.ledger.read_proposals())
        self.assertEqual(result["proposals"]["lapsed"], 1)
        self.assertEqual(result["deleted"], [])

    def test_a_person_marked_delete_is_recorded_and_carried_out_at_once(self):
        self.sonarr.series_by_id[7]["tags"] = [5]
        planned = {"series_id": 7, **self.CANDIDATE, "proposal": proposals.by_person(None, datetime.now(timezone.utc))}
        self.plan["proposed"].append(planned)
        _, result = self.execute(self.REVIEW_ALL)
        self.assertEqual([item["series_id"] for item in result["deleted"]], [7])
        self.assertEqual(result["proposals"]["marked_by_person"], 1)
        self.assertEqual(self.ledger.read_proposals()[7]["by"], "person")

    def test_a_persons_delete_is_withdrawn_when_the_tag_has_gone(self):
        """Undoing on the page between the plan and the run has to work."""
        self.due(7, by="person")
        code, result = self.execute(self.REVIEW_ALL)
        self.assertEqual(code, 0)
        self.assertEqual(result["deleted"], [])
        self.assertIn("cleanup-eligible was removed", result["skipped"][0]["why"])

    def test_a_persons_delete_goes_ahead_of_larger_titles_under_the_cap(self):
        self.due(6)
        self.resize(6, 90_000_000_000)
        self.sonarr.series_by_id[7]["tags"] = [5]
        self.due(7, by="person")
        _, result = self.execute(self.REVIEW_ALL, CLEANUP_MAX_DELETIONS="1")
        self.assertEqual([item["series_id"] for item in result["deleted"]], [7])
        self.assertEqual(result["deferred"], [6])

    def test_the_default_deletion_cap_is_unchanged(self):
        for item_id in (6, 7, 8):
            self.due(item_id)
        _, result = self.execute(self.REVIEW_ALL)
        self.assertEqual(result["deletion_cap"], 10)
        self.assertEqual(len(result["deleted"]), 3)

    def test_the_cap_can_be_set_from_the_environment(self):
        for item_id in (6, 7, 8):
            self.due(item_id)
        _, result = self.execute(self.REVIEW_ALL, CLEANUP_MAX_DELETIONS="1")
        self.assertEqual(result["deletion_cap"], 1)
        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(len(result["deferred"]), 2)

    def test_the_cap_takes_the_largest_first(self):
        for item_id in (6, 7, 8):
            self.due(item_id)
        self.resize(8, 90_000_000_000)
        _, result = self.execute(self.REVIEW_ALL, CLEANUP_MAX_DELETIONS="1")
        self.assertEqual([item["series_id"] for item in result["deleted"]], [8])

    def test_a_cap_that_cannot_be_read_stops_before_anything_is_done(self):
        self.due(6)
        with self.assertRaises(SystemExit):
            self.execute(self.REVIEW_ALL, CLEANUP_MAX_DELETIONS="lots")
        self.assertFalse((self.root / "r.json").exists())
        self.assertEqual(self.sonarr.tag_calls, [])

    def test_a_wait_that_cannot_be_read_stops_before_anything_is_done(self):
        with self.assertRaises(SystemExit):
            self.execute({1: "delete", 2: "review", 3: "review"}, CLEANUP_GRACE_DAYS="a week")
        self.assertFalse((self.root / "r.json").exists())
        self.assertEqual(self.ledger.read_proposals(), {})


if __name__ == "__main__":
    unittest.main()
