"""Regression cover for the fourth review of the TV fork.

A Plex item with no external id whose plays vanished, an import sweep that
stopped early and called itself complete, and a run whose safety checks could
not be read reporting a clean week.
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
from curatortv.__main__ import resolve_identities
from curatortv.clients import Sonarr, SourceError
from curatortv.evidence import build_crosswalk, resolve_viewing
from curatortv.ledger import Ledger
from curatortv.model import HistoryStatus

from .fixtures import FakeS3, FakeSonarr, FakeTautulli, days_ago, make_availability, make_show, play_row, series_record
from .test_clients import _Opener
from .test_stalestate import _Crosswalk, _metadata_client

IDLESS = {"guids": ["plex://show/5d9c086c46115600200aa2fe"], "title": "Test Show", "year": 2026}
RESOLVED = {"guids": ["tvdb://70001", "tmdb://1000"], "title": "Test Show", "year": 2026}


class IdlessPlexItemsTaint(unittest.TestCase):
    """An item Plex knows only by its own guid cannot be attributed, so it taints."""

    def rows(self):
        """One viewer on the id-less key, another on a key that resolves."""
        return {900: [play_row(900, 11, 95, 1)], 901: [play_row(901, 12, 95, 2)]}

    def test_build_crosswalk_marks_an_idless_item_unresolved(self):
        self.assertTrue(build_crosswalk({900: IDLESS})[900].get("unresolved"))
        self.assertFalse(build_crosswalk({901: RESOLVED})[901].get("unresolved"))

    def test_its_plays_taint_a_series_matched_through_another_key(self):
        ledger = _Crosswalk()
        crosswalk, unattributed = resolve_identities(
            _metadata_client({900: IDLESS, 901: RESOLVED}), ledger, self.rows()
        )
        self.assertEqual([r["grandparent_rating_key"] for r in unattributed], [900])
        result = resolve_viewing(
            make_show(), self.rows(), crosswalk, days_ago(400), make_availability(), unattributed_rows=unattributed
        )
        self.assertIs(result.status, HistoryStatus.UNRESOLVED)

    def test_an_idless_answer_is_asked_again_next_run(self):
        """Persisted, it would taint that title forever after Plex gained the ids."""
        ledger = _Crosswalk()
        resolve_identities(_metadata_client({900: IDLESS, 901: RESOLVED}), ledger, self.rows())
        self.assertNotIn(900, ledger.written)

    def test_a_cached_idless_entry_is_re_asked(self):
        ledger = _Crosswalk()
        ledger.written = {900: {"tvdb": None, "tmdb": None, "imdb": None, "title": "Test Show", "year": 2026}}
        crosswalk, _ = resolve_identities(_metadata_client({900: RESOLVED}), ledger, self.rows())
        self.assertEqual(crosswalk[900]["tvdb"], 70001)


def page(records, total):
    """One page of Sonarr's history endpoint."""
    return {"totalRecords": total, "records": records}


def imported(series_id):
    """One import record."""
    return {"eventType": "downloadFolderImported", "seriesId": series_id, "episodeId": 1, "date": "2026-03-01"}


class ImportSweepIsCompleteOrRaises(unittest.TestCase):
    """A short sweep loses first imports, and a lost first import restarts a window."""

    def test_an_empty_page_before_the_total_raises(self):
        client = Sonarr("http://s", "k", opener=_Opener([page([imported(1), imported(2)], 5), page([], 5)]))
        with self.assertRaises(SourceError):
            client.import_events(page_size=2)

    def test_more_records_than_the_page_budget_raises(self):
        client = Sonarr("http://s", "k", opener=_Opener([page([imported(1), imported(2)], 10)]))
        with self.assertRaises(SourceError):
            client.import_events(page_size=2, max_pages=3)

    def test_a_complete_sweep_still_answers(self):
        client = Sonarr("http://s", "k", opener=_Opener([page([imported(1), imported(2)], 3), page([imported(3)], 3)]))
        self.assertEqual(sorted(client.import_events(page_size=2)), [1, 2, 3])

    def test_the_default_budget_covers_the_live_library_with_headroom(self):
        """About 75k import rows live (2026-10-14); the budget must be several times that."""
        defaults = Sonarr.import_events.__defaults__
        self.assertGreaterEqual(defaults[0] * defaults[1], 4 * 75_000)


class UnavailableChecksAreNotACleanWeek(unittest.TestCase):
    """A deletion skipped because a check could not run is a failure, not a protection."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        candidate = {"origin": "feed", "availability_source": "sonarr-history", "history_status": "ok"}
        plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [{"series_id": 1, "episode_file_count": 10, **candidate}],
        }
        recs = {
            "plan_run_id": "plan-1",
            "recommendations": [{"series_id": 1, "verdict": "delete", "reason": "trending add nobody watched"}],
        }
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.root / "recs.json").write_text(json.dumps(recs), encoding="utf-8")
        self.sonarr = FakeSonarr({1: series_record(1)}, {1: "keep"})
        self.tautulli = FakeTautulli()
        sources = SimpleNamespace(
            sonarr=self.sonarr,
            tautulli=self.tautulli,
            seerr=None,
            ledger=Ledger(FakeS3(), "run-execute", dry_run=True),
            run_id="run-execute",
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self) -> tuple[int, dict]:
        """Run execute and return its exit code and result."""
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"),
            recommendations=str(self.root / "recs.json"),
            out=str(self.root / "r.json"),
            read_only=True,
        )
        with mock.patch.dict(os.environ, {"TAG_KEEPS_IN_DRY_RUN": "false"}):
            code = cli.cmd_execute(args)
        return code, json.loads((self.root / "r.json").read_text(encoding="utf-8"))

    def test_an_unreachable_activity_check_exits_10(self):
        self.tautulli.raise_error = SourceError("tautulli down")
        code, result = self.execute()
        self.assertEqual(code, 10)
        self.assertTrue(any(s.get("unavailable") for s in result["skipped"]))

    def test_an_unreadable_import_history_exits_10(self):
        self.sonarr.import_history_error = SourceError("history timed out")
        self.assertEqual(self.execute()[0], 10)

    def test_a_protection_skip_is_still_a_clean_run(self):
        self.sonarr.series_by_id[1]["tags"] = [1]
        code, result = self.execute()
        self.assertEqual(code, 0)
        self.assertIn("protection tag", result["skipped"][0]["why"])

    def test_a_new_import_is_still_a_clean_run(self):
        self.sonarr.import_history[1] = [
            {"eventType": "downloadFolderImported", "date": datetime.now(timezone.utc).isoformat()}
        ]
        self.assertEqual(self.execute()[0], 0)

    def test_the_digest_names_unavailable_checks(self):
        now = datetime.now(timezone.utc)
        plan = {"run_id": "p", "generated_at": now.isoformat(), "counts": {}}
        result = {"plan_run_id": "p", "skipped": [{"series_id": 1, "why": "x", "unavailable": True}]}
        self.assertIn(
            "1 deletion skipped: a safety check was unavailable", reporting.render(plan, result, None, now).push
        )


if __name__ == "__main__":
    unittest.main()
