"""Allow-list maintenance, and the one place this engine writes in a dry run.

A dry run may add ``keep`` for the rating bar, behind ``TAG_KEEPS_IN_DRY_RUN``,
because Reclaimerr protects anything carrying it; the film engine has the same
flag and its manifest leaves it off. The tag is applied only when the flag is
set and only where a person has not decided otherwise, and a dry run still never
reaches a delete. A judge's due keep writes no tag in any mode: it becomes a
decision that spares the series for ``CLEANUP_KEEP_DAYS``.
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

from .fixtures import FakeS3, FakeSonarr, FakeTautulli, make_show, propose, series_record

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
    """A dry run writes the tag only when the flag says it may."""

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
    """End to end through ``cmd_execute``: due keeps land, deletions do not."""

    CANDIDATE = {"origin": "feed", "availability_source": "sonarr-history", "history_status": "ok"}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        series = {
            1: series_record(1, tags=[8]),
            2: series_record(2, tags=[8], tvdb_id=70002, tmdb_id=2000, imdb_id="tt0000002"),
            3: series_record(3, tags=[5], tvdb_id=70003, tmdb_id=3000, imdb_id="tt0000003"),
            4: series_record(4, tags=[8], tvdb_id=70004, tmdb_id=4000, imdb_id="tt0000004"),
        }
        self.sonarr = FakeSonarr(series, dict(LABELS))
        self.ledger = Ledger(FakeS3(), "run-execute", dry_run=True)
        sources = SimpleNamespace(
            sonarr=self.sonarr, tautulli=FakeTautulli(), seerr=None, ledger=self.ledger, run_id="run-execute"
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, flag: str, age_days: float = 7.0, judged: str = "") -> dict:
        """Run execute --read-only in dry-run mode and return the result file.

        Three verdicts from an earlier run are on record: delete series 1, keep 2
        and keep 3 (which a person has since marked cleanup-eligible). ``judged``
        is this run's verdict on series 4, when it gives one.
        """
        plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [{"series_id": 4, **self.CANDIDATE}] if judged else [],
            "proposed": [
                propose(self.ledger, {"series_id": 1, **self.CANDIDATE}, "delete", age_days=age_days),
                propose(self.ledger, {"series_id": 2, **self.CANDIDATE}, "keep", age_days=age_days),
                propose(self.ledger, {"series_id": 3, **self.CANDIDATE}, "keep", age_days=age_days),
            ],
            "keep_tag_additions": [],
        }
        verdicts = [{"series_id": 4, "verdict": judged, "reason": "acclaimed completed series"}] if judged else []
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.root / "recs.json").write_text(
            json.dumps({"plan_run_id": "plan-1", "recommendations": verdicts}), encoding="utf-8"
        )
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"),
            recommendations=str(self.root / "recs.json"),
            out=str(self.root / "result.json"),
            read_only=True,
        )
        with mock.patch.dict(os.environ, {"TAG_KEEPS_IN_DRY_RUN": flag}):
            self.assertEqual(cli.cmd_execute(args), 0)
        return json.loads((self.root / "result.json").read_text(encoding="utf-8"))

    def test_due_judge_keeps_are_kept_for_a_year_and_nothing_is_written_to_sonarr(self):
        result = self.execute("true")
        self.assertEqual(self.sonarr.delete_calls, [])
        self.assertEqual(self.sonarr.tag_calls, [], "the flag is for the rating bar; a judge keep is never a tag")
        self.assertEqual((result["judge_keeps"]["due"], result["judge_keeps"]["kept"]), (2, 1))
        # Series 3 carries cleanup-eligible: a person's delete outranks the judge.
        self.assertEqual(result["judge_keeps"]["left_for_person"], [3])
        self.assertTrue(all(d.get("simulated") for d in result["deleted"]))
        self.assertIn("keep_tags_added", result)

    def test_a_kept_series_leaves_the_record_and_the_rehearsed_delete_stays(self):
        self.execute("true")
        self.assertEqual(sorted(self.ledger.read_proposals()), [1, 3])
        decision = self.ledger.read_decisions()[2]
        until = datetime.fromisoformat(decision["kept_until"])
        self.assertLess(abs((until - datetime.now(timezone.utc) - timedelta(days=365)).total_seconds()), 120)

    def test_without_the_flag_the_keep_is_still_a_decision_and_nothing_is_written(self):
        result = self.execute("false")
        self.assertEqual(self.sonarr.delete_calls, [])
        self.assertEqual(self.sonarr.tag_calls, [])
        self.assertEqual(result["judge_keeps"]["kept"], 1)
        self.assertEqual(sorted(self.ledger.read_proposals()), [1, 3])

    def test_keep_days_sets_how_long_the_keep_lasts(self):
        with mock.patch.dict(os.environ, {"CLEANUP_KEEP_DAYS": "30"}):
            self.execute("false")
        until = datetime.fromisoformat(self.ledger.read_decisions()[2]["reconsider_after"])
        self.assertLess(abs((until - datetime.now(timezone.utc) - timedelta(days=30)).total_seconds()), 120)

    def test_keep_days_of_zero_is_refused_before_anything_is_written(self):
        with mock.patch.dict(os.environ, {"CLEANUP_KEEP_DAYS": "0"}), self.assertRaises(SystemExit):
            self.execute("false")
        self.assertEqual(self.ledger.read_decisions(), {})

    def test_a_cleanup_keep_added_in_the_review_week_wins(self):
        self.sonarr.tags_by_id[9] = "cleanup-keep"
        self.sonarr.series_by_id[2]["tags"] = [8, 9]
        result = self.execute("true")
        self.assertEqual(result["judge_keeps"]["vetoed"], [2])
        self.assertNotIn(2, self.ledger.read_decisions())
        self.assertNotIn(2, self.ledger.read_proposals())
        self.assertEqual(self.sonarr.tag_calls, [])

    def test_a_keep_inside_its_wait_is_not_settled_even_with_the_flag(self):
        """Nothing shortens the week a person has to refuse the keep, or keep it for good."""
        result = self.execute("true", age_days=1)
        self.assertEqual(self.sonarr.tag_calls, [])
        self.assertEqual(result["judge_keeps"]["due"], 0)
        self.assertEqual(self.ledger.read_decisions(), {})

    def test_a_keep_given_this_run_is_put_on_record_and_not_tagged(self):
        result = self.execute("true", age_days=1, judged="keep")
        self.assertEqual(self.sonarr.tag_calls, [])
        self.assertEqual(result["proposals"]["recorded"], {"delete": 0, "keep": 1})
        self.assertEqual(self.ledger.read_proposals()[4]["verdict"], "keep")


if __name__ == "__main__":
    unittest.main()
