"""Regression cover for the second review of the TV fork.

Each case is one where the engine would have reported a safe, quiet run while
being wrong: a watched series read as a verified zero, a new season deleted the
week it arrived, and a keep recorded as settled when its tag never landed. Only
the rating bar writes ``keep`` now; a judge's keep is a stored decision and needs
no tag to land.
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
from curatortv import reporting
from curatortv.evidence import resolve_viewing
from curatortv.executor import execute
from curatortv.ledger import Ledger
from curatortv.model import HistoryStatus

from .fixtures import (
    FakeS3,
    FakeSonarr,
    FakeTautulli,
    days_ago,
    make_availability,
    make_show,
    propose,
    series_record,
)
from .test_executor import TAGS, assessment

PLANNED_AT = datetime.now(timezone.utc) - timedelta(minutes=30)


class LaterSeasonPlaysTaintTheZero(unittest.TestCase):
    """A Tautulli episode row's ``year`` is the episode's, not the show's premiere."""

    def test_a_later_season_play_on_a_retired_key_still_taints(self):
        show = make_show(title="Vikings", year=2013, tvdb_id=1, tmdb_id=None, imdb_id=None)
        rows = [{"grandparent_title": "Vikings", "year": 2019, "user_id": 11, "percent_complete": 99}]
        result = resolve_viewing(show, {}, {}, days_ago(400), make_availability(), unattributed_rows=rows)
        self.assertIs(result.status, HistoryStatus.UNRESOLVED)

    def test_a_verified_show_year_still_separates_a_reboot(self):
        show = make_show(title="Doctor Who", year=2005, tvdb_id=1, tmdb_id=None, imdb_id=None)
        rows = [{"grandparent_title": "Doctor Who", "grandparent_year": 1963, "year": 1965}]
        result = resolve_viewing(show, {}, {}, days_ago(400), make_availability(), unattributed_rows=rows)
        self.assertIs(result.status, HistoryStatus.OK)


class NewEpisodesSincePlanningBlockTheDelete(unittest.TestCase):
    """A continuing show whose window restarted after the plan must not be deleted."""

    def run_batch(self, sonarr, planned_files=None):
        """Execute one series in act mode."""
        return execute(
            [assessment()],
            sonarr,
            FakeTautulli(),
            Ledger(FakeS3(), "r1", dry_run=False),
            TAGS,
            dry_run=False,
            planned_at=PLANNED_AT,
            planned_files=planned_files,
        )

    def test_an_import_after_the_plan_skips_the_delete(self):
        sonarr = FakeSonarr({1: series_record(status="continuing")}, TAGS)
        sonarr.import_history[1] = [
            {"eventType": "downloadFolderImported", "date": datetime.now(timezone.utc).isoformat(), "episodeId": 99}
        ]
        result = self.run_batch(sonarr)
        self.assertEqual(sonarr.delete_calls, [])
        self.assertIn("imported since the plan", result.skipped[0]["why"])

    def test_a_grown_inventory_skips_the_delete(self):
        sonarr = FakeSonarr({1: series_record(status="continuing", episode_files=12)}, TAGS)
        result = self.run_batch(sonarr, planned_files={1: 10})
        self.assertEqual(sonarr.delete_calls, [])
        self.assertIn("episodes on disk grew", result.skipped[0]["why"])

    def test_an_unreadable_history_skips_the_delete(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        sonarr.import_history_error = cli.SourceError("history timed out")
        result = self.run_batch(sonarr)
        self.assertEqual(sonarr.delete_calls, [])
        self.assertTrue(result.skipped)

    def test_old_imports_and_an_unchanged_inventory_still_delete(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        sonarr.import_history[1] = [
            {"eventType": "downloadFolderImported", "date": days_ago(300).isoformat(), "episodeId": 1}
        ]
        self.run_batch(sonarr, planned_files={1: 10})
        self.assertEqual(sonarr.delete_calls, [1])


class UnconfirmedKeepTagsStayUndecided(unittest.TestCase):
    """A rating-bar tag that never landed fails the run; a judge's keep is settled without any tag."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        now = datetime.now(timezone.utc)
        self.sonarr = FakeSonarr(
            {
                1: series_record(1, tags=[4]),
                2: series_record(2, tags=[4], tvdb_id=70002, tmdb_id=2000, imdb_id="tt0000002"),
            },
            dict(TAGS),
        )
        self.store = FakeS3()
        ledger = Ledger(self.store, "run-execute", dry_run=True)
        candidate = {"origin": "feed", "availability_source": "sonarr-history", "history_status": "ok"}
        # Both keeps were handed down by an earlier run and their wait is over.
        plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (now - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [],
            "proposed": [propose(ledger, {"series_id": n, **candidate}, "keep") for n in (1, 2)],
            # The rating bar asks for the same two series to be tagged.
            "keep_tag_additions": [{"series_id": n} for n in (1, 2)],
        }
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.root / "recs.json").write_text(
            json.dumps({"plan_run_id": "plan-1", "recommendations": []}), encoding="utf-8"
        )
        sources = SimpleNamespace(
            sonarr=self.sonarr,
            tautulli=FakeTautulli(),
            seerr=None,
            ledger=ledger,
            run_id="run-execute",
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self) -> tuple[int, dict]:
        """Run execute and return its exit code and result file."""
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"),
            recommendations=str(self.root / "recs.json"),
            out=str(self.root / "result.json"),
            read_only=True,
        )
        with mock.patch.dict(os.environ, {"TAG_KEEPS_IN_DRY_RUN": "true"}):
            code = cli.cmd_execute(args)
        return code, json.loads((self.root / "result.json").read_text(encoding="utf-8"))

    def test_an_unconfirmed_rating_bar_tag_fails_the_run(self):
        # The editor answers, but series 2 never gains the tag.
        real_add = self.sonarr.add_tag

        def partial(series_ids, tag_id):
            status, body = real_add([s for s in series_ids if s != 2], tag_id)
            return status, body

        self.sonarr.add_tag = partial
        code, result = self.execute()
        self.assertEqual(code, 8)
        self.assertEqual(result["keep_tags_added"]["unconfirmed"], [2])

    def test_a_judge_keep_is_settled_with_no_tag_even_when_tag_writes_fail(self):
        """The judge's year is ledger state: a failing tag editor neither blocks it nor is asked."""
        calls = []
        self.sonarr.add_tag = lambda series_ids, tag_id: calls.append(series_ids) or (500, None)
        code, result = self.execute()
        self.assertEqual(code, 8, "the rating bar's failed tags still fail the run")
        self.assertEqual(sorted(result["keep_tags_added"]["unconfirmed"]), [1, 2])
        self.assertEqual(calls, [[1, 2]], "only the rating bar wrote")
        self.assertEqual(result["judge_keeps"]["kept"], 2)
        self.assertIn("dry-run/decisions/1.json", self.store.store)
        self.assertIn("dry-run/decisions/2.json", self.store.store)
        self.assertFalse([k for k in self.store.store if "/proposals/" in k])

    def test_the_digest_reports_unconfirmed_keeps(self):
        now = datetime.now(timezone.utc)
        plan = {"run_id": "p", "generated_at": now.isoformat(), "counts": {}}
        result = {"plan_run_id": "p", "keep_tags_added": {"requested": 2, "applied": 1, "unconfirmed": [2]}}
        digest = reporting.render(plan, result, None, now)
        self.assertIn("1 keep tag not confirmed", digest.push)


if __name__ == "__main__":
    unittest.main()
