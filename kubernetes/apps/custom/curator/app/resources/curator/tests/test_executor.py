"""Recommendation validation, live re-checks and the outcomes of a deletion."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from curator.clients import RequestSystem, SourceError
from curator.executor import (
    execute,
    late_evidence,
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
from .test_clients import _Opener

TAGS = {1: "keep", 2: "keep-review", 3: "cleanup-keep", 4: "src-tmdb-popular"}

# The moment the plan was made; the late-evidence gate compares against it.
PLANNED_AT = datetime.now(timezone.utc) - timedelta(minutes=30)


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

    def test_a_scalar_in_place_of_a_recommendation_is_rejected(self):
        """A syntactically valid array can still hold `[null]` or `["review"]`."""
        accepted, rejected = validate_recommendations([None, "review", 7], self.candidates)
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 3)

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
        _, rejected = validate_recommendations([{"movie_id": 1, "verdict": "delete", "reason": "bad"}], self.candidates)
        self.assertIn("without a stated reason", rejected[0]["why"])

    def test_keep_without_a_reason_is_rejected(self):
        """A keep becomes a permanent tag, so it needs a reason as much as a delete does."""
        _, rejected = validate_recommendations([{"movie_id": 1, "verdict": "keep", "reason": "good"}], self.candidates)
        self.assertEqual(rejected[0]["why"], "keep without a stated reason")

    def test_review_needs_no_long_reason(self):
        accepted, _ = validate_recommendations([{"movie_id": 1, "verdict": "review", "reason": ""}], self.candidates)
        self.assertEqual(len(accepted), 1)

    def test_a_boolean_is_not_an_id(self):
        _, rejected = validate_recommendations(
            [{"movie_id": True, "verdict": "delete", "reason": "a perfectly good reason"}], self.candidates
        )
        self.assertIn("not an integer", rejected[0]["why"])

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
        ok, detail = recycle_bin_ready({"recycleBin": "/recycle", "recycleBinCleanupDays": 14})
        self.assertTrue(ok)
        self.assertIn("14", detail)

    def test_empty_path_blocks(self):
        ok, detail = recycle_bin_ready({"recycleBin": "", "recycleBinCleanupDays": 14})
        self.assertFalse(ok)
        self.assertIn("unrecoverable", detail)

    def test_zero_retention_blocks(self):
        ok, _ = recycle_bin_ready({"recycleBin": "/recycle", "recycleBinCleanupDays": 0})
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
            [assessment()], radarr, FakeTautulli(), ledger, TAGS, set(), dry_run=True, planned_at=PLANNED_AT
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
            [assessment()], radarr, FakeTautulli(), ledger, TAGS, set(), dry_run=False, planned_at=PLANNED_AT
        )
        self.assertEqual(radarr.deleted, [1])
        self.assertEqual(len(result.deleted), 1)
        self.assertEqual(self.s3.loads("runs/run-test/outcome/1.json")["status"], "deleted")

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
            planned_at=PLANNED_AT,
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
            planned_at=PLANNED_AT,
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
            planned_at=PLANNED_AT,
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
            planned_at=PLANNED_AT,
        )
        self.assertEqual(len(result.uncertain), 1)
        self.assertEqual(radarr.deleted, [])
        self.assertEqual(self.s3.loads("runs/run-test/outcome/1.json")["status"], "uncertain")

    def test_unavailable_activity_check_skips_rather_than_proceeds(self):
        """An unavailable safety check is an unresolved one, not a passed one.

        Asserted on the outcome rather than the wording: Tautulli backs both the
        current-playback check and the late-completion check, so which message
        fires first is an implementation detail. What matters is that nothing is
        deleted and the run says why.
        """
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
            planned_at=PLANNED_AT,
        )
        self.assertEqual(radarr.deleted, [])
        self.assertEqual(result.deleted, [])
        self.assertTrue(any("could not" in item.get("why", "") for item in result.skipped))

    def test_partial_failure_does_not_stop_the_batch(self):
        radarr = FakeRadarr({1: movie_record(1), 2: movie_record(2, tmdb_id=2000)}, TAGS)
        radarr.delete_behaviour[1] = "http-500"
        result = execute(
            [assessment(1), assessment(2)],
            radarr,
            FakeTautulli(),
            self.ledger(False),
            TAGS,
            set(),
            dry_run=False,
            planned_at=PLANNED_AT,
        )
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.deleted), 1)
        self.assertEqual(radarr.deleted, [2])


class _Seerr:  # pylint: disable=too-few-public-methods
    """A request system holding a fixed set of requests, or failing."""

    def __init__(self, requests: dict[int, dict] | None = None, error: Exception | None = None):
        self.requests = requests or {}
        self.error = error

    def movie_requests(self) -> dict[int, dict]:
        """Every request, keyed by tmdb id."""
        if self.error:
            raise self.error
        return self.requests


def stamp(minutes_from_plan: int) -> str:
    """An ISO timestamp that many minutes after the plan was made."""
    return (PLANNED_AT + timedelta(minutes=minutes_from_plan)).isoformat()


class RequestsAroundThePlanTests(unittest.TestCase):
    """A request is on the candidate's record; only one made since the plan stops a deletion."""

    def run_one(self, seerr, tautulli=None, approved=None):
        """Execute one approved film (tmdb id 1000) in a dry run."""
        return execute(
            approved or [assessment()],
            FakeRadarr({1: movie_record()}, TAGS),
            tautulli or FakeTautulli(),
            Ledger(FakeS3(), run_id="run-test", dry_run=True),
            TAGS,
            set(),
            dry_run=True,
            seerr=seerr,
            crosswalk={5000: {"tmdb": 1000, "imdb": "tt0000001"}},
            planned_at=PLANNED_AT,
        )

    def test_a_request_made_before_the_plan_does_not_stop_the_deletion(self):
        """It was a protection once. Now the film waited out its window with the request on record."""
        seerr = _Seerr({1000: {"requested_by": "Sam", "created_at": stamp(-60 * 24 * 200)}})
        result = self.run_one(seerr)
        self.assertEqual([d["movie_id"] for d in result.deleted], [1])
        self.assertEqual(result.skipped, [])

    def test_a_request_made_since_the_plan_stops_it(self):
        seerr = _Seerr({1000: {"requested_by": "Alex", "created_at": stamp(5)}})
        result = self.run_one(seerr)
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.skipped[0]["why"], "requested by Alex since the plan was made")

    def test_a_second_request_since_the_plan_is_not_hidden_by_the_first(self):
        """A requested film can be a candidate now, so its newest request is the one that counts."""
        seerr = _Seerr(
            {
                1000: {
                    "requested_by": "Sam",
                    "created_at": stamp(-60 * 24 * 200),
                    "latest_by": "Alex",
                    "latest_at": stamp(5),
                }
            }
        )
        result = self.run_one(seerr)
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.skipped[0]["why"], "requested by Alex since the plan was made")

    def test_the_real_client_surfaces_the_newer_of_two_requests(self):
        """Whichever order the request system lists them in."""

        def request(who: str, created: str) -> dict:
            return {
                "type": "movie",
                "media": {"tmdbId": 1000},
                "requestedBy": {"displayName": who},
                "createdAt": created,
            }

        old, new = request("Sam", stamp(-60 * 24 * 200)), request("Alex", stamp(5))
        for order in ([old, new], [new, old]):
            with self.subTest(first=order[0]["requestedBy"]["displayName"]):
                payload = {"pageInfo": {"results": 2}, "results": order}
                # Read twice: once before the batch, once immediately before the deletion.
                seerr = RequestSystem("http://r", "k", opener=_Opener([payload, payload]))
                result = self.run_one(seerr)
                self.assertEqual(result.deleted, [])
                self.assertEqual(result.skipped[0]["why"], "requested by Alex since the plan was made")

    def test_a_request_that_cannot_be_dated_stops_it(self):
        """Not provably before the plan, so it is treated as since."""
        for request in ({"requested_by": "Sam", "created_at": None}, {"requested_by": "Sam", "undated": True}):
            with self.subTest(request=request):
                result = self.run_one(_Seerr({1000: request}))
                self.assertEqual(result.deleted, [])
                self.assertIn("since the plan was made", result.skipped[0]["why"])

    def test_a_request_for_another_film_stops_nothing(self):
        seerr = _Seerr({4242: {"requested_by": "Alex", "created_at": stamp(5)}})
        self.assertEqual(len(self.run_one(seerr).deleted), 1)

    def test_an_unreachable_request_system_skips_rather_than_proceeds(self):
        result = self.run_one(_Seerr(error=SourceError("seerr down")))
        self.assertEqual(result.deleted, [])
        self.assertTrue(result.skipped[0]["unavailable"])

    def test_requests_known_before_the_plan_are_not_read_as_late_evidence(self):
        seerr = _Seerr({1000: {"requested_by": "Sam", "created_at": stamp(-10)}})
        requests, _, known = late_evidence(FakeTautulli(), seerr, {}, PLANNED_AT)
        self.assertEqual((requests, known), ({}, True))


class CompletionsSinceThePlanTests(unittest.TestCase):
    """Two completers protect a film; the second one can arrive after the plan."""

    def finished(self, user_id: int, minutes_from_plan: int) -> dict:
        """A completed play of the film's Plex item, stopped relative to the plan."""
        when = int((PLANNED_AT + timedelta(minutes=minutes_from_plan)).timestamp())
        return {"rating_key": 5000, "user_id": user_id, "percent_complete": 96, "stopped": when}

    def run_one(self, rows):
        """Execute a candidate one person had finished when the plan was made."""
        tautulli = FakeTautulli()
        tautulli.history_rows = rows
        one_completer = replace(assessment(), viewing=make_viewing(completers=1))
        return execute(
            [one_completer],
            FakeRadarr({1: movie_record()}, TAGS),
            tautulli,
            Ledger(FakeS3(), run_id="run-test", dry_run=True),
            TAGS,
            set(),
            dry_run=True,
            crosswalk={5000: {"tmdb": 1000, "imdb": "tt0000001"}},
            planned_at=PLANNED_AT,
        )

    def test_a_second_person_finishing_it_since_the_plan_stops_the_deletion(self):
        result = self.run_one([self.finished(100, -5000), self.finished(200, 5)])
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.skipped[0]["why"], "completed by someone since the plan was made")

    def test_the_one_completion_the_plan_already_knew_does_not_stop_it(self):
        result = self.run_one([self.finished(100, -5000)])
        self.assertEqual([d["movie_id"] for d in result.deleted], [1])


class ReconcileTests(unittest.TestCase):
    """An interrupted run is resolved by looking, not by retrying."""

    def test_interrupted_delete_that_landed_is_recorded_as_deleted(self):
        s3 = FakeS3()
        ledger = Ledger(s3, run_id="run-2", dry_run=False)
        radarr = FakeRadarr({}, TAGS)
        resolved = reconcile([{"run_id": "run-1", "movie": {"movie_id": 7}}], radarr, ledger)
        self.assertEqual(resolved[0]["status"], "deleted")

    def test_interrupted_delete_that_did_not_land_is_recorded_as_not_applied(self):
        s3 = FakeS3()
        ledger = Ledger(s3, run_id="run-2", dry_run=False)
        radarr = FakeRadarr({7: movie_record(7)}, TAGS)
        resolved = reconcile([{"run_id": "run-1", "movie": {"movie_id": 7}}], radarr, ledger)
        self.assertEqual(resolved[0]["status"], "not-applied")
        self.assertEqual(radarr.deleted, [], "reconciliation must never re-issue the delete")


if __name__ == "__main__":
    unittest.main()
