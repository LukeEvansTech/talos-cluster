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


class ExecuteTests(unittest.TestCase):
    """Deferred candidates owe no verdict, and the deletion cap comes from the environment."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        candidate = {"origin": "unknown", "availability_source": "sonarr-history", "history_status": "ok"}
        plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "judge_limit": 3,
            "candidates": [{"series_id": n, **candidate} for n in (1, 2, 3)],
            "judge_deferred": [{"series_id": n, **candidate} for n in (4, 5)],
        }
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        series = {
            n: series_record(n, tvdb_id=70000 + n, tmdb_id=1000 * n, imdb_id=f"tt000000{n}") for n in (1, 2, 3, 4, 5)
        }
        self.sonarr = FakeSonarr(series, {1: "keep"})
        sources = SimpleNamespace(
            sonarr=self.sonarr,
            tautulli=FakeTautulli(),
            seerr=None,
            ledger=Ledger(FakeS3(), "run-execute", dry_run=True),
            run_id="run-execute",
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, verdicts: dict[int, str], cap: str | None = None) -> tuple[int, dict]:
        """Run execute against ``{series_id: verdict}`` and return its exit code and result."""
        recommendations = [
            {"series_id": series_id, "verdict": verdict, "reason": "never played and no standing to name"}
            for series_id, verdict in verdicts.items()
        ]
        path = self.root / "recs.json"
        path.write_text(json.dumps({"plan_run_id": "plan-1", "recommendations": recommendations}), encoding="utf-8")
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"), recommendations=str(path), out=str(self.root / "r.json"), read_only=True
        )
        with mock.patch.dict(os.environ, {} if cap is None else {"CLEANUP_MAX_DELETIONS": cap}):
            if cap is None:
                os.environ.pop("CLEANUP_MAX_DELETIONS", None)
            code = cli.cmd_execute(args)
        return code, json.loads((self.root / "r.json").read_text(encoding="utf-8"))

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

    def test_the_default_deletion_cap_is_unchanged(self):
        _, result = self.execute({1: "delete", 2: "delete", 3: "delete"})
        self.assertEqual(result["deletion_cap"], 10)
        self.assertEqual(len(result["deleted"]), 3)

    def test_the_cap_can_be_set_from_the_environment(self):
        _, result = self.execute({1: "delete", 2: "delete", 3: "delete"}, cap="1")
        self.assertEqual(result["deletion_cap"], 1)
        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(len(result["deferred"]), 2)

    def test_a_cap_that_cannot_be_read_stops_before_anything_is_done(self):
        path = self.root / "recs.json"
        path.write_text(json.dumps({"plan_run_id": "plan-1", "recommendations": []}), encoding="utf-8")
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"), recommendations=str(path), out=str(self.root / "r.json"), read_only=True
        )
        with mock.patch.dict(os.environ, {"CLEANUP_MAX_DELETIONS": "lots"}), self.assertRaises(SystemExit):
            cli.cmd_execute(args)
        self.assertFalse((self.root / "r.json").exists())
        self.assertEqual(self.sonarr.tag_calls, [])


if __name__ == "__main__":
    unittest.main()
