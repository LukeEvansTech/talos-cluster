"""Allow-list maintenance, and the one place this engine writes in a dry run.

The film engine writes nothing in a dry run. This one may add ``keep``, behind
``TAG_KEEPS_IN_DRY_RUN``, because Reclaimerr protects anything carrying it. The
tests below pin both halves of that: the tag is applied only when the flag is
set and only where a person has not decided otherwise, and a dry run still
never reaches a delete.
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
from unittest import mock

from curatortv import __main__ as cli
from curatortv.__main__ import apply_keep_tags
from curatortv.ledger import Ledger
from curatortv.planner import keep_tag_applies, keep_tag_targets

from .fixtures import FakeS3, FakeSonarr, FakeTautulli, make_show, series_record

LABELS = {1: "keep", 5: "cleanup-eligible", 6: "cleanup-dismissed", 7: "keep-review", 8: "kometa"}


class KeepTagTargetTests(unittest.TestCase):
    """Add-only, and never on top of somebody's decision."""

    def test_series_over_the_bar_is_tagged(self):
        show = make_show(tags=frozenset(), imdb_votes=6000, imdb_score=7.0)
        self.assertEqual(keep_tag_targets([show]), [show])

    def test_thin_votes_do_not_qualify(self):
        self.assertEqual(keep_tag_targets([make_show(tags=frozenset(), imdb_votes=1200, imdb_score=8.1)]), [])

    def test_low_score_does_not_qualify(self):
        self.assertEqual(keep_tag_targets([make_show(tags=frozenset(), imdb_votes=90000, imdb_score=6.4)]), [])

    def test_missing_rating_does_not_qualify(self):
        self.assertEqual(keep_tag_targets([make_show(tags=frozenset(), imdb_votes=None, imdb_score=None)]), [])

    def test_already_tagged_is_not_retagged(self):
        self.assertEqual(keep_tag_targets([make_show(tags={"keep"}, imdb_votes=6000, imdb_score=7.0)]), [])

    def test_keep_review_item_is_never_promoted(self):
        self.assertEqual(keep_tag_targets([make_show(tags={"keep-review"}, imdb_votes=650166, imdb_score=8.5)]), [])

    def test_human_eligibility_is_not_silently_reversed(self):
        self.assertFalse(keep_tag_applies(make_show(tags={"cleanup-eligible"})))

    def test_human_dismissal_is_not_silently_reversed(self):
        self.assertFalse(keep_tag_applies(make_show(tags={"cleanup-dismissed"})))


class DryRunKeepTagTests(unittest.TestCase):
    """The deliberate deviation from the film engine."""

    def sonarr(self, tags=None) -> FakeSonarr:
        """One series, with the given live tag ids."""
        return FakeSonarr({1: series_record(tags=tags or [])}, dict(LABELS))

    def apply(self, sonarr, dry_run=True, flag=False):
        """Apply a keep to series 1, re-checked against its live record."""
        return apply_keep_tags(
            sonarr,
            [{"series_id": 1}],
            dry_run=dry_run,
            live={s["id"]: s for s in sonarr.all_series()},
            tag_labels=LABELS,
            tag_in_dry_run=flag,
        )

    def test_dry_run_applies_nothing_without_the_flag(self):
        sonarr = self.sonarr()
        outcome = self.apply(sonarr, flag=False)
        self.assertEqual(outcome["applied"], 0)
        self.assertEqual(sonarr.tag_calls, [])

    def test_dry_run_applies_the_keep_tag_when_the_flag_is_set(self):
        sonarr = self.sonarr()
        outcome = self.apply(sonarr, flag=True)
        self.assertEqual(outcome["applied"], 1)
        self.assertEqual(sonarr.tag_calls, [([1], 1)])
        self.assertIn(1, sonarr.series_by_id[1]["tags"])

    def test_cleanup_eligible_blocks_it_even_with_the_flag(self):
        sonarr = self.sonarr(tags=[5])
        outcome = self.apply(sonarr, flag=True)
        self.assertEqual(outcome["withdrawn"], 1)
        self.assertEqual(sonarr.tag_calls, [])

    def test_cleanup_dismissed_and_keep_review_block_it_too(self):
        for tag in (6, 7):
            sonarr = self.sonarr(tags=[tag])
            self.assertEqual(self.apply(sonarr, flag=True)["withdrawn"], 1)
            self.assertEqual(sonarr.tag_calls, [])

    def test_a_missing_keep_tag_is_created_first(self):
        sonarr = FakeSonarr({1: series_record()}, {8: "kometa"})
        outcome = apply_keep_tags(sonarr, [{"series_id": 1}], dry_run=True, tag_in_dry_run=True)
        self.assertEqual(outcome["applied"], 1)
        self.assertIn("keep", sonarr.tags_by_id.values())

    def test_act_mode_applies_without_the_flag(self):
        self.assertEqual(self.apply(self.sonarr(), dry_run=False)["applied"], 1)


class DryRunExecuteNeverDeletes(unittest.TestCase):
    """End to end through ``cmd_execute``: keeps land, deletions do not."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        now = datetime.now(timezone.utc)
        series = {
            1: series_record(1, tags=[8]),
            2: series_record(2, tags=[8], tvdb_id=70002, tmdb_id=2000, imdb_id="tt0000002"),
            3: series_record(3, tags=[5], tvdb_id=70003, tmdb_id=3000, imdb_id="tt0000003"),
        }
        self.sonarr = FakeSonarr(series, dict(LABELS))
        self.ledger = Ledger(FakeS3(), "run-execute", dry_run=True)

        def candidate(series_id):
            return {
                "series_id": series_id,
                "origin": "feed",
                "availability_source": "sonarr-history",
                "history_status": "ok",
            }

        plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (now - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [candidate(1), candidate(2), candidate(3)],
            "keep_tag_additions": [],
        }
        recommendations = {
            "plan_run_id": "plan-1",
            "recommendations": [
                {"series_id": 1, "verdict": "delete", "reason": "trending add nobody watched"},
                {"series_id": 2, "verdict": "keep", "reason": "acclaimed completed series"},
                {"series_id": 3, "verdict": "keep", "reason": "acclaimed completed series"},
            ],
        }
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.root / "recs.json").write_text(json.dumps(recommendations), encoding="utf-8")
        sources = SimpleNamespace(
            sonarr=self.sonarr, tautulli=FakeTautulli(), seerr=None, ledger=self.ledger, run_id="run-execute"
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, flag: str) -> dict:
        """Run execute --read-only in dry-run mode and return the result file."""
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"),
            recommendations=str(self.root / "recs.json"),
            out=str(self.root / "result.json"),
            read_only=True,
        )
        with mock.patch.dict(os.environ, {"TAG_KEEPS_IN_DRY_RUN": flag}):
            self.assertEqual(cli.cmd_execute(args), 0)
        return json.loads((self.root / "result.json").read_text(encoding="utf-8"))

    def test_judge_keeps_are_tagged_and_nothing_is_deleted(self):
        result = self.execute("true")
        self.assertEqual(self.sonarr.delete_calls, [])
        self.assertEqual(self.sonarr.tag_calls, [([2], 1)])
        self.assertEqual(result["judge_keeps_tagged"]["applied"], 1)
        # Series 3 carries cleanup-eligible: a person's decision outranks the judge.
        self.assertEqual(result["judge_keeps_tagged"]["withdrawn"], 1)
        self.assertTrue(all(d.get("simulated") for d in result["deleted"]))
        self.assertIn("keep_tags_added", result)

    def test_without_the_flag_nothing_is_written(self):
        result = self.execute("false")
        self.assertEqual(self.sonarr.delete_calls, [])
        self.assertEqual(self.sonarr.tag_calls, [])
        self.assertEqual(result["judge_keeps_tagged"]["applied"], 0)


if __name__ == "__main__":
    unittest.main()
