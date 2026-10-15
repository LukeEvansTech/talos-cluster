"""Regression cover for the fixes ported back from the TV fork's reviews.

Each case is a check that read as passed while it could not see the thing it
guards: plays behind an older Plex key, a person's dismissal made after the
plan, a session whose film could not be identified, and a week judged by nobody
or checked by nothing.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from curator import __main__ as cli
from curator import reporting
from curator.clients import SourceError, Tautulli
from curator.evidence import resolve_viewing
from curator.executor import revalidate
from curator.ledger import Ledger
from curator.model import HistoryStatus

from .fixtures import FakeRadarr, FakeS3, FakeTautulli, days_ago, make_availability, make_film, play_row
from .test_executor import movie_record
from .test_failclosed import _Opener, envelope


class EveryMatchingKeyCounts(unittest.TestCase):
    """A film can sit behind several Plex keys, each knowing different ids."""

    def view(self, crosswalk):
        """Viewing for the default film over one finished play per key."""
        rows = {key: [play_row(key, 100 + i, 95, i)] for i, key in enumerate(crosswalk)}
        return resolve_viewing(make_film(), rows, crosswalk, days_ago(400), make_availability())

    def test_an_older_key_known_only_by_imdb_still_counts(self):
        result = self.view(
            {
                5000: {"tmdb": 1000, "imdb": "tt0000001"},
                4000: {"tmdb": None, "imdb": "tt0000001"},
            }
        )
        self.assertEqual(result.distinct_completers, 2)
        self.assertEqual(sorted(result.rating_keys), [4000, 5000])
        self.assertEqual(result.identity_via, "tmdb")

    def test_a_key_whose_ids_conflict_is_excluded(self):
        """Sharing an IMDb id while naming another TMDb film is a different film."""
        result = self.view(
            {
                5000: {"tmdb": 1000, "imdb": "tt0000001"},
                6000: {"tmdb": 9999, "imdb": "tt0000001"},
            }
        )
        self.assertEqual(result.rating_keys, (5000,))
        self.assertEqual(result.distinct_completers, 1)
        self.assertIs(result.status, HistoryStatus.OK)


class DismissalAfterPlanningBlocks(unittest.TestCase):
    """A person's tag added after the plan must stop the delete."""

    LABELS = {1: "keep", 2: "keep-review", 3: "cleanup-keep", 6: "cleanup-dismissed"}

    def test_each_human_tag_added_since_planning_blocks(self):
        for tag_id, label in self.LABELS.items():
            radarr = FakeRadarr({1: movie_record(tags=[tag_id])}, self.LABELS)
            ok, why, _ = revalidate(1, radarr, self.LABELS, set(), set())
            self.assertFalse(ok, label)
            self.assertIn(label, why)


class UnidentifiableSessionsFailClosed(unittest.TestCase):
    """A session we cannot tie to a film could be the film about to be deleted."""

    def session(self, metadata, session=None):
        """A client whose only session's metadata is ``metadata``."""
        session = session or {"media_type": "movie", "rating_key": 77}
        return Tautulli(
            "http://t",
            "k",
            opener=_Opener([envelope("success", {"sessions": [session]}), metadata]),
        )

    def test_missing_film_metadata_raises(self):
        with self.assertRaises(SourceError):
            self.session(envelope("error", None)).now_playing_ids()

    def test_film_metadata_with_no_usable_guid_raises(self):
        with self.assertRaises(SourceError):
            self.session(envelope("success", {"guids": ["plex://movie/abc"]})).now_playing_ids()

    def test_a_film_session_with_no_rating_key_raises(self):
        client = Tautulli(
            "http://t", "k", opener=_Opener([envelope("success", {"sessions": [{"media_type": "movie"}]})])
        )
        with self.assertRaises(SourceError):
            client.now_playing_ids()

    def test_a_resolvable_session_still_answers(self):
        ids = self.session(envelope("success", {"guids": ["tmdb://1000", "imdb://tt0000001"]})).now_playing_ids()
        self.assertEqual(ids, {("tmdb", "1000"), ("imdb", "tt0000001")})

    def test_an_episode_session_is_not_a_film(self):
        client = Tautulli(
            "http://t",
            "k",
            opener=_Opener([envelope("success", {"sessions": [{"media_type": "episode", "rating_key": 5}]})]),
        )
        self.assertEqual(client.now_playing_ids(), set())

    def test_a_transport_failure_still_raises(self):
        with self.assertRaises(SourceError):
            self.session(urllib.error.URLError("timeout")).now_playing_ids()


class _Radarr(FakeRadarr):
    """FakeRadarr with the collection listing execute reads."""

    def collections(self) -> list[dict]:
        """No collections, so none is monitored."""
        return []


class ExecuteExitCodes(unittest.TestCase):
    """A run that judged nothing, or could not check, must not reset the staleness alert."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        candidate = {"origin": "feed", "availability_source": "radarr-history", "history_status": "ok"}
        plan = {
            "run_id": "plan-1",
            "mode": "dry-run",
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [{"movie_id": 1, **candidate}, {"movie_id": 2, **candidate}],
        }
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.radarr = _Radarr({1: movie_record(1), 2: movie_record(2, tmdb_id=2000)}, {1: "keep"})
        self.tautulli = FakeTautulli()
        sources = SimpleNamespace(
            radarr=self.radarr,
            tautulli=self.tautulli,
            seerr=None,
            ledger=Ledger(FakeS3(), "run-execute", dry_run=True),
            run_id="run-execute",
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def execute(self, recommendations) -> tuple[int, dict]:
        """Run execute against these verdicts and return its exit code and result."""
        path = self.root / "recs.json"
        path.write_text(json.dumps({"plan_run_id": "plan-1", "recommendations": recommendations}), encoding="utf-8")
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"), recommendations=str(path), out=str(self.root / "r.json"), read_only=True
        )
        code = cli.cmd_execute(args)
        return code, json.loads((self.root / "r.json").read_text(encoding="utf-8"))

    REVIEW_BOTH = [
        {"movie_id": 1, "verdict": "review", "reason": "unsure"},
        {"movie_id": 2, "verdict": "review", "reason": "unsure"},
    ]
    DELETE_ONE = [
        {"movie_id": 1, "verdict": "delete", "reason": "trending add nobody watched"},
        {"movie_id": 2, "verdict": "review", "reason": "unsure"},
    ]

    def test_an_empty_answer_is_not_a_success(self):
        self.assertEqual(self.execute([])[0], 9)

    def test_a_rejected_recommendation_is_not_a_success(self):
        recs = [
            {"movie_id": 1, "verdict": "delete", "reason": "x"},
            {"movie_id": 2, "verdict": "review", "reason": "unsure"},
        ]
        self.assertEqual(self.execute(recs)[0], 9)

    def test_a_fully_judged_week_still_succeeds(self):
        self.assertEqual(self.execute(self.REVIEW_BOTH)[0], 0)

    def test_an_unreachable_activity_check_exits_10(self):
        self.tautulli.raise_error = SourceError("tautulli down")
        code, result = self.execute(self.DELETE_ONE)
        self.assertEqual(code, 10)
        self.assertTrue(any(s.get("unavailable") for s in result["skipped"]))

    def test_a_protection_skip_is_still_a_clean_run(self):
        self.radarr.movies_by_id[1]["tags"] = [1]
        code, result = self.execute(self.DELETE_ONE)
        self.assertEqual(code, 0)
        self.assertIn("protection tag", result["skipped"][0]["why"])

    def test_the_digest_names_unavailable_checks(self):
        now = datetime.now(timezone.utc)
        plan = {"run_id": "p", "generated_at": now.isoformat(), "counts": {}}
        result = {"plan_run_id": "p", "skipped": [{"movie_id": 1, "why": "x", "unavailable": True}]}
        self.assertIn(
            "1 deletion skipped: a safety check was unavailable", reporting.render(plan, result, None, now).push
        )


if __name__ == "__main__":
    unittest.main()
