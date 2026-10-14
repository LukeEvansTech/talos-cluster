"""Recommendation validation, live re-checks and the outcomes of a deletion."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from curatortv.clients import SourceError
from curatortv.executor import (
    execute,
    reconcile,
    recycle_bin_ready,
    restore_snapshot,
    revalidate,
    validate_recommendations,
)
from curatortv.ledger import Ledger
from curatortv.model import Assessment, Outcome

from .fixtures import (
    FakeS3,
    FakeSonarr,
    FakeTautulli,
    feed_provenance,
    make_availability,
    make_show,
    make_viewing,
    series_record,
)

TAGS = {1: "keep", 2: "keep-review", 3: "cleanup-keep", 4: "kometa"}

# The moment the plan was made; the late-evidence gate compares against it.
PLANNED_AT = datetime.now(timezone.utc) - timedelta(minutes=30)


def assessment(series_id=1, size=50_000_000_000, rating_keys=(5000,), tvdb_id=70001):
    """An approved assessment ready for execution."""
    viewing = replace(make_viewing(), rating_keys=rating_keys)
    return Assessment(
        show=make_show(series_id=series_id, size_bytes=size, tvdb_id=tvdb_id),
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
            [{"series_id": 1, "verdict": "delete", "reason": "trending add that never caught on"}], self.candidates
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])

    def test_a_scalar_in_place_of_a_recommendation_is_rejected(self):
        accepted, rejected = validate_recommendations([None, "review", 7], self.candidates)
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 3)

    def test_unknown_series_id_is_rejected(self):
        _, rejected = validate_recommendations(
            [{"series_id": 99, "verdict": "delete", "reason": "looks like filler to me"}], self.candidates
        )
        self.assertIn("not in this run's candidate set", rejected[0]["why"])

    def test_a_movie_id_is_not_a_series_id(self):
        """A verdict shaped for the film engine names nothing here."""
        _, rejected = validate_recommendations(
            [{"movie_id": 1, "verdict": "delete", "reason": "a perfectly good reason"}], self.candidates
        )
        self.assertIn("series_id missing", rejected[0]["why"])

    def test_bad_verdict_is_rejected(self):
        _, rejected = validate_recommendations(
            [{"series_id": 1, "verdict": "obliterate", "reason": "because"}], self.candidates
        )
        self.assertIn("unknown verdict", rejected[0]["why"])

    def test_delete_without_a_reason_is_rejected(self):
        _, rejected = validate_recommendations(
            [{"series_id": 1, "verdict": "delete", "reason": "bad"}], self.candidates
        )
        self.assertIn("delete without a stated reason", rejected[0]["why"])

    def test_keep_without_a_reason_is_rejected(self):
        """A keep becomes a permanent tag, so it needs a reason as much as a delete."""
        _, rejected = validate_recommendations([{"series_id": 1, "verdict": "keep", "reason": "good"}], self.candidates)
        self.assertIn("keep without a stated reason", rejected[0]["why"])

    def test_review_needs_no_long_reason(self):
        accepted, _ = validate_recommendations([{"series_id": 1, "verdict": "review", "reason": "?"}], self.candidates)
        self.assertEqual(len(accepted), 1)

    def test_non_integer_id_is_rejected(self):
        _, rejected = validate_recommendations(
            [{"series_id": "1", "verdict": "delete", "reason": "a perfectly good reason"}], self.candidates
        )
        self.assertIn("not an integer", rejected[0]["why"])

    def test_the_second_verdict_for_a_series_is_rejected(self):
        accepted, rejected = validate_recommendations(
            [
                {"series_id": 1, "verdict": "delete", "reason": "trending add, nobody watched"},
                {"series_id": 1, "verdict": "keep", "reason": "well-known completed series"},
            ],
            self.candidates,
        )
        self.assertEqual([a["verdict"] for a in accepted], ["delete"])
        self.assertEqual(rejected[0]["why"], "judged more than once")


class RecycleBinTests(unittest.TestCase):
    """deleteFiles only means 'recoverable' when a bin is configured."""

    def test_configured_bin_is_ready(self):
        ok, detail = recycle_bin_ready({"recycleBin": "/recycle", "recycleBinCleanupDays": 14})
        self.assertTrue(ok)
        self.assertIn("14", detail)

    def test_empty_path_blocks(self):
        ok, detail = recycle_bin_ready({"recycleBin": "", "recycleBinCleanupDays": 14})
        self.assertFalse(ok)
        self.assertIn("unrecoverable", detail)

    def test_zero_retention_blocks(self):
        self.assertFalse(recycle_bin_ready({"recycleBin": "/recycle", "recycleBinCleanupDays": 0})[0])

    def test_missing_config_blocks(self):
        self.assertFalse(recycle_bin_ready({})[0])


class RevalidationTests(unittest.TestCase):
    """Everything is re-checked against live state, not against the plan."""

    def test_clean_series_passes(self):
        ok, _, live = revalidate(1, FakeSonarr({1: series_record()}, TAGS), TAGS, set())
        self.assertTrue(ok)
        self.assertEqual(live["id"], 1)

    def test_tag_added_after_planning_blocks(self):
        ok, why, _ = revalidate(1, FakeSonarr({1: series_record(tags=[1])}, TAGS), TAGS, set())
        self.assertFalse(ok)
        self.assertIn("protection tag added since planning", why)

    def test_active_playback_blocks_on_any_external_id(self):
        """A first viewing has no play history, so identity must come from the record."""
        for playing in ({("tvdb", "70001")}, {("tmdb", "1000")}, {("imdb", "tt0000001")}):
            ok, why, _ = revalidate(1, FakeSonarr({1: series_record()}, TAGS), TAGS, playing)
            self.assertFalse(ok)
            self.assertIn("watching it right now", why)

    def test_active_playback_of_another_series_does_not_block(self):
        self.assertTrue(revalidate(1, FakeSonarr({1: series_record()}, TAGS), TAGS, {("tvdb", "9999")})[0])

    def test_already_gone_blocks(self):
        ok, why, _ = revalidate(1, FakeSonarr({}, TAGS), TAGS, set())
        self.assertFalse(ok)
        self.assertIn("no longer in Sonarr", why)

    def test_files_disappeared_blocks(self):
        self.assertFalse(revalidate(1, FakeSonarr({1: series_record(episode_files=0)}, TAGS), TAGS, set())[0])

    def test_upcoming_since_planning_blocks(self):
        ok, why, _ = revalidate(1, FakeSonarr({1: series_record(status="upcoming")}, TAGS), TAGS, set())
        self.assertFalse(ok)
        self.assertIn("upcoming", why)

    def test_restore_snapshot_captures_what_recreating_needs(self):
        live = series_record()
        live.update({"path": "/tv/Test Show", "qualityProfileId": 10, "monitored": True})
        snapshot = restore_snapshot(live)
        for field in ("tvdbId", "path", "qualityProfileId", "monitored", "tags", "seasons"):
            self.assertIn(field, snapshot)
        self.assertEqual(snapshot["seasons"], [{"seasonNumber": 1, "monitored": True}])


class ExecutionTests(unittest.TestCase):
    """What the routine records, and when it refuses to be sure."""

    def setUp(self):
        self.s3 = FakeS3()

    def ledger(self, dry_run: bool) -> Ledger:
        """A ledger backed by the in-memory store."""
        return Ledger(self.s3, run_id="run-test", dry_run=dry_run)

    def run_one(self, sonarr, dry_run=False, tautulli=None, approved=None):
        """Execute a batch with test defaults."""
        return execute(
            approved or [assessment()],
            sonarr,
            tautulli or FakeTautulli(),
            self.ledger(dry_run),
            TAGS,
            dry_run=dry_run,
            planned_at=PLANNED_AT,
        )

    def test_dry_run_writes_intent_but_never_an_outcome(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        result = self.run_one(sonarr, dry_run=True)
        self.assertEqual(len(result.deleted), 1)
        self.assertTrue(result.deleted[0]["simulated"])
        self.assertEqual(sonarr.delete_calls, [], "dry run must not call delete at all")
        keys = self.s3.list_keys("")
        self.assertTrue(any("dry-run/runs/run-test/intent/" in k for k in keys))
        self.assertFalse(any("/outcome/" in k for k in keys), "dry run must record no completed actions")

    def test_act_deletes_and_records_the_outcome(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        result = self.run_one(sonarr)
        self.assertEqual(sonarr.deleted, [1])
        self.assertEqual(len(result.deleted), 1)
        self.assertEqual(self.s3.loads("runs/run-test/outcome/1.json")["status"], "deleted")

    def test_http_error_is_a_failure_not_a_deletion(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        sonarr.delete_behaviour[1] = "http-500"
        result = self.run_one(sonarr)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.deleted), 0)

    def test_success_status_with_a_surviving_record_is_uncertain(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        sonarr.delete_behaviour[1] = "silent-noop"
        result = self.run_one(sonarr)
        self.assertEqual(len(result.uncertain), 1)
        self.assertEqual(len(result.deleted), 0)

    def test_timeout_that_did_apply_is_confirmed_by_re_reading(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        error = SourceError("timed out")
        error.applied = True
        sonarr.delete_behaviour[1] = error
        result = self.run_one(sonarr)
        self.assertEqual(len(result.deleted), 1)
        self.assertIn("after a transport error", result.deleted[0]["note"])

    def test_timeout_that_did_not_apply_is_uncertain_not_retried(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        sonarr.delete_behaviour[1] = SourceError("timed out")
        result = self.run_one(sonarr)
        self.assertEqual(len(result.uncertain), 1)
        self.assertEqual(sonarr.delete_calls, [1])
        self.assertEqual(self.s3.loads("runs/run-test/outcome/1.json")["status"], "uncertain")

    def test_unavailable_activity_check_skips_rather_than_proceeds(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        result = self.run_one(sonarr, tautulli=FakeTautulli(raise_error=SourceError("tautulli down")))
        self.assertEqual(sonarr.delete_calls, [])
        self.assertEqual(result.deleted, [])
        self.assertTrue(any("could not" in item.get("why", "") for item in result.skipped))

    def test_partial_failure_does_not_stop_the_batch(self):
        sonarr = FakeSonarr({1: series_record(1), 2: series_record(2, tvdb_id=70002, tmdb_id=2000)}, TAGS)
        sonarr.delete_behaviour[1] = "http-500"
        result = self.run_one(sonarr, approved=[assessment(1), assessment(2, tvdb_id=70002)])
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.deleted), 1)
        self.assertEqual(sonarr.deleted, [2])


class ReconcileTests(unittest.TestCase):
    """An interrupted run is resolved by looking, not by retrying."""

    def test_interrupted_delete_that_landed_is_recorded_as_deleted(self):
        ledger = Ledger(FakeS3(), run_id="run-2", dry_run=False)
        resolved = reconcile([{"run_id": "run-1", "series": {"series_id": 7}}], FakeSonarr({}, TAGS), ledger)
        self.assertEqual(resolved[0]["status"], "deleted")

    def test_interrupted_delete_that_did_not_land_is_recorded_as_not_applied(self):
        ledger = Ledger(FakeS3(), run_id="run-2", dry_run=False)
        sonarr = FakeSonarr({7: series_record(7)}, TAGS)
        resolved = reconcile([{"run_id": "run-1", "series": {"series_id": 7}}], sonarr, ledger)
        self.assertEqual(resolved[0]["status"], "not-applied")
        self.assertEqual(sonarr.delete_calls, [], "reconciliation must never re-issue the delete")


if __name__ == "__main__":
    unittest.main()
