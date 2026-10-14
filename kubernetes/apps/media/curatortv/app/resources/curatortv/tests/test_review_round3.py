"""Regression cover for the third review of the TV fork.

Each case is a check that read as passed while it could not see the thing it
guards: plays behind an older Plex key, a person's dismissal made after the
plan, a session whose show could not be identified, and a week judged by nobody.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from curatortv import __main__ as cli
from curatortv.clients import SourceError, Tautulli
from curatortv.evidence import resolve_viewing
from curatortv.executor import revalidate
from curatortv.ledger import Ledger
from curatortv.model import HistoryStatus

from .fixtures import FakeS3, FakeSonarr, FakeTautulli, days_ago, make_availability, make_show, play_row, series_record
from .test_failclosed import _Opener, envelope


class EveryMatchingKeyCounts(unittest.TestCase):
    """A show can sit behind several Plex keys, each knowing different ids."""

    def view(self, crosswalk):
        """Viewing for the default show over one play per key."""
        rows = {key: [play_row(key, 100 + i, 95, i)] for i, key in enumerate(crosswalk)}
        return resolve_viewing(make_show(), rows, crosswalk, days_ago(400), make_availability())

    def test_an_older_key_known_only_by_tmdb_still_counts(self):
        result = self.view(
            {
                5000: {"tvdb": 70001, "tmdb": 1000, "imdb": "tt0000001"},
                4000: {"tvdb": None, "tmdb": 1000, "imdb": None},
            }
        )
        self.assertEqual(result.distinct_viewers, 2)
        self.assertEqual(sorted(result.rating_keys), [4000, 5000])

    def test_an_older_key_known_only_by_imdb_still_counts(self):
        result = self.view(
            {
                5000: {"tvdb": 70001, "tmdb": None, "imdb": None},
                4000: {"tvdb": None, "tmdb": None, "imdb": "tt0000001"},
            }
        )
        self.assertEqual(result.distinct_viewers, 2)

    def test_a_key_whose_ids_conflict_is_excluded(self):
        """Sharing a TMDb id while naming another TVDB series is a different show."""
        result = self.view(
            {
                5000: {"tvdb": 70001, "tmdb": 1000, "imdb": None},
                6000: {"tvdb": 99999, "tmdb": 1000, "imdb": None},
            }
        )
        self.assertEqual(result.rating_keys, (5000,))
        self.assertEqual(result.distinct_viewers, 1)
        self.assertIs(result.status, HistoryStatus.OK)


class DismissalAfterPlanningBlocks(unittest.TestCase):
    """A person's tag added after the plan must stop the delete."""

    LABELS = {1: "keep", 2: "keep-review", 3: "cleanup-keep", 6: "cleanup-dismissed"}

    def test_each_human_tag_added_since_planning_blocks(self):
        for tag_id in (1, 2, 3, 6):
            sonarr = FakeSonarr({1: series_record(tags=[tag_id])}, self.LABELS)
            ok, why, _ = revalidate(1, sonarr, self.LABELS, set())
            self.assertFalse(ok, self.LABELS[tag_id])
            self.assertIn(self.LABELS[tag_id], why)


class UnidentifiableSessionsFailClosed(unittest.TestCase):
    """A session we cannot tie to a show could be the show about to be deleted."""

    def session(self, metadata):
        """A client whose only session's show metadata is ``metadata``."""
        return Tautulli(
            "http://t",
            "k",
            opener=_Opener(
                [
                    envelope("success", {"sessions": [{"media_type": "episode", "grandparent_rating_key": 77}]}),
                    metadata,
                ]
            ),
        )

    def test_missing_show_metadata_raises(self):
        with self.assertRaises(SourceError):
            self.session(envelope("error", None)).now_playing_ids()

    def test_show_metadata_with_no_usable_guid_raises(self):
        with self.assertRaises(SourceError):
            self.session(envelope("success", {"guids": ["plex://show/abc"]})).now_playing_ids()

    def test_a_resolvable_session_still_answers(self):
        ids = self.session(envelope("success", {"guids": ["tvdb://75710"]})).now_playing_ids()
        self.assertEqual(ids, {("tvdb", "75710")})

    def test_a_transport_failure_still_raises(self):
        with self.assertRaises(SourceError):
            self.session(urllib.error.URLError("timeout")).now_playing_ids()


class UnjudgedWeeksAreNotSuccesses(unittest.TestCase):
    """A run that judged nothing must not reset the staleness alert."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        candidate = {"origin": "feed", "availability_source": "sonarr-history", "history_status": "ok"}
        plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [{"series_id": 1, **candidate}, {"series_id": 2, **candidate}],
        }
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        sonarr = FakeSonarr(
            {1: series_record(1), 2: series_record(2, tvdb_id=70002, tmdb_id=2000, imdb_id="tt0000002")}
        )
        sources = SimpleNamespace(
            sonarr=sonarr,
            tautulli=FakeTautulli(),
            seerr=None,
            ledger=Ledger(FakeS3(), "run-execute", dry_run=True),
            run_id="run-execute",
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, recommendations) -> int:
        """Run execute against these verdicts and return its exit code."""
        path = self.root / "recs.json"
        path.write_text(json.dumps({"plan_run_id": "plan-1", "recommendations": recommendations}), encoding="utf-8")
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"), recommendations=str(path), out=str(self.root / "r.json"), read_only=True
        )
        with mock.patch.dict(os.environ, {"TAG_KEEPS_IN_DRY_RUN": "false"}):
            return cli.cmd_execute(args)

    def test_an_empty_answer_is_not_a_success(self):
        self.assertEqual(self.execute([]), 9)

    def test_a_rejected_recommendation_is_not_a_success(self):
        recs = [
            {"series_id": 1, "verdict": "review", "reason": "unsure"},
            {"series_id": 2, "verdict": "keep", "reason": "ok"},
        ]
        self.assertEqual(self.execute(recs), 9)

    def test_a_fully_judged_week_still_succeeds(self):
        recs = [
            {"series_id": 1, "verdict": "review", "reason": "unsure"},
            {"series_id": 2, "verdict": "review", "reason": "unsure"},
        ]
        self.assertEqual(self.execute(recs), 0)


if __name__ == "__main__":
    unittest.main()
