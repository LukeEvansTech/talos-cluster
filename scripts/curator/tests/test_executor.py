"""Recommendation validation, live re-checks and the outcomes of a deletion."""

from __future__ import annotations

import unittest
from dataclasses import replace

from curator.clients import SourceError
from curator.executor import (
    execute,
    reconcile,
    recycle_bin_ready,
    restore_snapshot,
    revalidate,
    validate_recommendations,
)
from curator.ledger import Ledger
from curator.model import Assessment, Outcome

from .fixtures import (
    FakeRadarr,
    FakeS3,
    FakeTautulli,
    feed_provenance,
    make_availability,
    make_film,
    make_viewing,
)

TAGS = {1: "keep", 2: "keep-review", 3: "cleanup-keep", 4: "src-tmdb-popular"}


def movie_record(movie_id=1, tags=None, tmdb_id=1000, status="released", has_file=True):
    """A minimal live Radarr record."""
    return {
        "id": movie_id,
        "tmdbId": tmdb_id,
        "title": "Test Film",
        "year": 2026,
        "status": status,
        "hasFile": has_file,
        "sizeOnDisk": 10_000_000_000,
        "added": "2026-05-01T00:00:00Z",
        "tags": tags or [],
        "ratings": {"imdb": {"value": 6.0, "votes": 4000}},
    }


def assessment(movie_id=1, size=10_000_000_000, rating_keys=(5000,)):
    """An approved assessment ready for execution."""
    viewing = replace(make_viewing(), rating_keys=rating_keys)
    return Assessment(
        film=make_film(movie_id=movie_id, size_bytes=size),
        outcome=Outcome.CANDIDATE,
        provenance=feed_provenance(),
        availability=make_availability(),
        viewing=viewing,
    )


class RecommendationValidationTests(unittest.TestCase):
    """The two halves must agree about what was being judged."""

    def setUp(self):
        self.candidates = {1: object(), 2: object()}

    def test_valid_recommendation_is_accepted(self):
        accepted, rejected = validate_recommendations(
            [
                {
                    "movie_id": 1,
                    "verdict": "delete",
                    "reason": "feed filler, nobody played it",
                }
            ],
            self.candidates,
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])

    def test_unknown_movie_id_is_rejected(self):
        _, rejected = validate_recommendations(
            [
                {
                    "movie_id": 99,
                    "verdict": "delete",
                    "reason": "looks like filler to me",
                }
            ],
            self.candidates,
        )
        self.assertIn("not in this run's candidate set", rejected[0]["why"])

    def test_bad_verdict_is_rejected(self):
        _, rejected = validate_recommendations(
            [{"movie_id": 1, "verdict": "obliterate", "reason": "because"}],
            self.candidates,
        )
        self.assertIn("unknown verdict", rejected[0]["why"])

    def test_delete_without_a_reason_is_rejected(self):
        _, rejected = validate_recommendations(
            [{"movie_id": 1, "verdict": "delete", "reason": "bad"}], self.candidates
        )
        self.assertIn("without a stated reason", rejected[0]["why"])

    def test_non_integer_id_is_rejected(self):
        _, rejected = validate_recommendations(
            [
                {
                    "movie_id": "1",
                    "verdict": "delete",
                    "reason": "a perfectly good reason",
                }
            ],
            self.candidates,
        )
        self.assertIn("not an integer", rejected[0]["why"])


class RecycleBinTests(unittest.TestCase):
    """deleteFiles only means 'recoverable' when a bin is configured."""

    def test_configured_bin_is_ready(self):
        ok, detail = recycle_bin_ready(
            {"recycleBin": "/recycle", "recycleBinCleanupDays": 14}
        )
        self.assertTrue(ok)
        self.assertIn("14", detail)

    def test_empty_path_blocks(self):
        ok, detail = recycle_bin_ready({"recycleBin": "", "recycleBinCleanupDays": 14})
        self.assertFalse(ok)
        self.assertIn("unrecoverable", detail)

    def test_zero_retention_blocks(self):
        ok, _ = recycle_bin_ready(
            {"recycleBin": "/recycle", "recycleBinCleanupDays": 0}
        )
        self.assertFalse(ok)

    def test_missing_config_blocks(self):
        self.assertFalse(recycle_bin_ready({})[0])


class RevalidationTests(unittest.TestCase):
    """Everything is re-checked against live state, not against the plan."""

    def test_clean_film_passes(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        ok, _, live = revalidate(1, radarr, TAGS, set(), set())
        self.assertTrue(ok)
        self.assertEqual(live["id"], 1)

    def test_tag_added_after_planning_blocks(self):
        radarr = FakeRadarr({1: movie_record(tags=[1])}, TAGS)
        ok, why, _ = revalidate(1, radarr, TAGS, set(), set())
        self.assertFalse(ok)
        self.assertIn("protection tag added since planning", why)

    def test_collection_monitored_since_planning_blocks(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        ok, why, _ = revalidate(1, radarr, TAGS, {1000}, set())
        self.assertFalse(ok)
        self.assertIn("monitored collection", why)

    def test_active_playback_blocks_on_external_id(self):
        """A first viewing has no play history, so identity must come from the record."""
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        ok, why, _ = revalidate(1, radarr, TAGS, set(), {("tmdb", "1000")})
        self.assertFalse(ok)
        self.assertIn("watching it right now", why)

    def test_active_playback_of_another_film_does_not_block(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        ok, _, _ = revalidate(1, radarr, TAGS, set(), {("tmdb", "9999")})
        self.assertTrue(ok)

    def test_already_gone_blocks(self):
        radarr = FakeRadarr({}, TAGS)
        ok, why, _ = revalidate(1, radarr, TAGS, set(), set())
        self.assertFalse(ok)
        self.assertIn("no longer in Radarr", why)

    def test_file_disappeared_blocks(self):
        radarr = FakeRadarr({1: movie_record(has_file=False)}, TAGS)
        self.assertFalse(revalidate(1, radarr, TAGS, set(), set())[0])

    def test_restore_snapshot_captures_what_recreating_needs(self):
        live = movie_record()
        live.update(
            {
                "path": "/films/Test Film (2026)",
                "qualityProfileId": 10,
                "monitored": True,
            }
        )
        snapshot = restore_snapshot(live)
        for field in ("tmdbId", "path", "qualityProfileId", "monitored", "tags"):
            self.assertIn(field, snapshot)


class ExecutionTests(unittest.TestCase):
    """What the routine records, and when it refuses to be sure."""

    def setUp(self):
        self.s3 = FakeS3()

    def ledger(self, dry_run: bool) -> Ledger:
        """A ledger backed by the in-memory store."""
        return Ledger(self.s3, run_id="run-test", dry_run=dry_run)

    def test_dry_run_writes_intent_but_never_an_outcome(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        ledger = self.ledger(dry_run=True)
        result = execute(
            [assessment()], radarr, FakeTautulli(), ledger, TAGS, set(), dry_run=True
        )
        self.assertEqual(len(result.deleted), 1)
        self.assertTrue(result.deleted[0]["simulated"])
        self.assertEqual(radarr.deleted, [], "dry run must not delete anything")
        keys = self.s3.list_keys("")
        self.assertTrue(any("dry-run/runs/run-test/intent/" in k for k in keys))
        self.assertFalse(
            any("/outcome/" in k for k in keys),
            "dry run must record no completed actions",
        )

    def test_act_deletes_and_records_the_outcome(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        ledger = self.ledger(dry_run=False)
        result = execute(
            [assessment()], radarr, FakeTautulli(), ledger, TAGS, set(), dry_run=False
        )
        self.assertEqual(radarr.deleted, [1])
        self.assertEqual(len(result.deleted), 1)
        self.assertEqual(
            self.s3.loads("runs/run-test/outcome/1.json")["status"], "deleted"
        )

    def test_http_error_is_a_failure_not_a_deletion(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        radarr.delete_behaviour[1] = "http-500"
        result = execute(
            [assessment()],
            radarr,
            FakeTautulli(),
            self.ledger(False),
            TAGS,
            set(),
            dry_run=False,
        )
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.deleted), 0)

    def test_success_status_with_a_surviving_record_is_uncertain(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        radarr.delete_behaviour[1] = "silent-noop"
        result = execute(
            [assessment()],
            radarr,
            FakeTautulli(),
            self.ledger(False),
            TAGS,
            set(),
            dry_run=False,
        )
        self.assertEqual(len(result.uncertain), 1)
        self.assertEqual(len(result.deleted), 0)

    def test_timeout_that_did_apply_is_confirmed_by_re_reading(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        error = SourceError("timed out")
        error.applied = True
        radarr.delete_behaviour[1] = error
        result = execute(
            [assessment()],
            radarr,
            FakeTautulli(),
            self.ledger(False),
            TAGS,
            set(),
            dry_run=False,
        )
        self.assertEqual(len(result.deleted), 1)
        self.assertIn("after a transport error", result.deleted[0]["note"])

    def test_timeout_that_did_not_apply_is_uncertain_not_retried(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        radarr.delete_behaviour[1] = SourceError("timed out")
        result = execute(
            [assessment()],
            radarr,
            FakeTautulli(),
            self.ledger(False),
            TAGS,
            set(),
            dry_run=False,
        )
        self.assertEqual(len(result.uncertain), 1)
        self.assertEqual(radarr.deleted, [])
        self.assertEqual(
            self.s3.loads("runs/run-test/outcome/1.json")["status"], "uncertain"
        )

    def test_unavailable_activity_check_skips_rather_than_proceeds(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        tautulli = FakeTautulli(raise_error=SourceError("tautulli down"))
        result = execute(
            [assessment()],
            radarr,
            tautulli,
            self.ledger(False),
            TAGS,
            set(),
            dry_run=False,
        )
        self.assertEqual(radarr.deleted, [])
        self.assertTrue(
            any(
                "could not confirm nobody is watching" in s.get("why", "")
                for s in result.skipped
            )
        )

    def test_partial_failure_does_not_stop_the_batch(self):
        radarr = FakeRadarr(
            {1: movie_record(1), 2: movie_record(2, tmdb_id=2000)}, TAGS
        )
        radarr.delete_behaviour[1] = "http-500"
        result = execute(
            [assessment(1), assessment(2)],
            radarr,
            FakeTautulli(),
            self.ledger(False),
            TAGS,
            set(),
            dry_run=False,
        )
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.deleted), 1)
        self.assertEqual(radarr.deleted, [2])


class ReconcileTests(unittest.TestCase):
    """An interrupted run is resolved by looking, not by retrying."""

    def test_interrupted_delete_that_landed_is_recorded_as_deleted(self):
        s3 = FakeS3()
        ledger = Ledger(s3, run_id="run-2", dry_run=False)
        radarr = FakeRadarr({}, TAGS)
        resolved = reconcile(
            [{"run_id": "run-1", "movie": {"movie_id": 7}}], radarr, ledger
        )
        self.assertEqual(resolved[0]["status"], "deleted")

    def test_interrupted_delete_that_did_not_land_is_recorded_as_not_applied(self):
        s3 = FakeS3()
        ledger = Ledger(s3, run_id="run-2", dry_run=False)
        radarr = FakeRadarr({7: movie_record(7)}, TAGS)
        resolved = reconcile(
            [{"run_id": "run-1", "movie": {"movie_id": 7}}], radarr, ledger
        )
        self.assertEqual(resolved[0]["status"], "not-applied")
        self.assertEqual(
            radarr.deleted, [], "reconciliation must never re-issue the delete"
        )


if __name__ == "__main__":
    unittest.main()
