"""Recommendation validation, live re-checks and the outcomes of a deletion."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from curatortv.clients import RequestSystem, SourceError
from curatortv.executor import (
    execute,
    late_evidence,
    reconcile,
    recycle_bin_ready,
    restore_snapshot,
    revalidate,
    validate_recommendations,
    volatile_block,
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
from .test_clients import _Opener

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

    def test_a_persons_delete_needs_cleanup_eligible_still_on_the_series(self):
        """A person's delete rests on the tag alone; taking it off during the run withdraws it."""
        tags = {**TAGS, 9: "cleanup-eligible"}
        ok, why, _ = revalidate(1, FakeSonarr({1: series_record()}, tags), tags, set(), by_person=True)
        self.assertFalse(ok)
        self.assertIn("cleanup-eligible was removed", why)
        ok, _, _ = revalidate(1, FakeSonarr({1: series_record(tags=[9])}, tags), tags, set(), by_person=True)
        self.assertTrue(ok)

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


class _Seerr:  # pylint: disable=too-few-public-methods
    """A request system holding a fixed set of requests, or failing."""

    def __init__(self, requests: dict | None = None, error: Exception | None = None):
        self.requests = requests or {}
        self.error = error

    def tv_requests(self) -> dict:
        """Every request, keyed by ``(source, id)``."""
        if self.error:
            raise self.error
        return self.requests


def stamp(minutes_from_plan: int) -> str:
    """An ISO timestamp that many minutes after the plan was made."""
    return (PLANNED_AT + timedelta(minutes=minutes_from_plan)).isoformat()


class RequestsAroundThePlanTests(unittest.TestCase):
    """A request is on the candidate's record; only one made since the plan stops a deletion."""

    def run_one(self, seerr, tautulli=None, approved=None):
        """Execute one approved series (tvdb id 70001) in a dry run."""
        return execute(
            approved or [assessment()],
            FakeSonarr({1: series_record()}, TAGS),
            tautulli or FakeTautulli(),
            Ledger(FakeS3(), run_id="run-test", dry_run=True),
            TAGS,
            dry_run=True,
            seerr=seerr,
            crosswalk={5000: {"tvdb": 70001, "tmdb": 1000, "imdb": "tt0000001"}},
            planned_at=PLANNED_AT,
        )

    def test_a_request_made_before_the_plan_does_not_stop_the_deletion(self):
        """It was a protection once. Now the series waited out its window with the request on record."""
        seerr = _Seerr({("tvdb", 70001): {"requested_by": "Sam", "created_at": stamp(-60 * 24 * 200)}})
        result = self.run_one(seerr)
        self.assertEqual([d["series_id"] for d in result.deleted], [1])
        self.assertEqual(result.skipped, [])

    def test_a_request_made_since_the_plan_stops_it(self):
        seerr = _Seerr({("tvdb", 70001): {"requested_by": "Alex", "created_at": stamp(5)}})
        result = self.run_one(seerr)
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.skipped[0]["why"], "requested by Alex since the plan was made")

    def test_a_later_seasons_request_is_not_hidden_by_the_first(self):
        """A show is requested a season at a time, and a requested series can be a candidate now."""
        record = {
            "requested_by": "Sam",
            "created_at": stamp(-60 * 24 * 200),
            "latest_by": "Alex",
            "latest_at": stamp(5),
        }
        result = self.run_one(_Seerr({("tvdb", 70001): record}))
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.skipped[0]["why"], "requested by Alex since the plan was made")

    def test_the_real_client_surfaces_the_newer_of_two_requests(self):
        """Whichever order the request system lists them in, and whichever id the later one carries."""

        def request(who: str, created: str, media: dict) -> dict:
            return {"type": "tv", "media": media, "requestedBy": {"displayName": who}, "createdAt": created}

        old = request("Sam", stamp(-60 * 24 * 200), {"tvdbId": 70001, "tmdbId": 1000})
        for label, new in (
            ("both ids", request("Alex", stamp(5), {"tvdbId": 70001, "tmdbId": 1000})),
            ("tmdb only", request("Alex", stamp(5), {"tmdbId": 1000})),
        ):
            for order in ([old, new], [new, old]):
                with self.subTest(later=label, first=order[0]["requestedBy"]["displayName"]):
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
                result = self.run_one(_Seerr({("tvdb", 70001): request}))
                self.assertEqual(result.deleted, [])
                self.assertIn("since the plan was made", result.skipped[0]["why"])

    def test_a_request_for_another_series_stops_nothing(self):
        seerr = _Seerr({("tvdb", 4242): {"requested_by": "Alex", "created_at": stamp(5)}})
        self.assertEqual(len(self.run_one(seerr).deleted), 1)

    def test_an_unreachable_request_system_skips_rather_than_proceeds(self):
        result = self.run_one(_Seerr(error=SourceError("seerr down")))
        self.assertEqual(result.deleted, [])
        self.assertTrue(result.skipped[0]["unavailable"])

    def test_requests_known_before_the_plan_are_not_read_as_late_evidence(self):
        seerr = _Seerr({("tvdb", 70001): {"requested_by": "Sam", "created_at": stamp(-10)}})
        requests, _, known, _ = late_evidence(FakeTautulli(), seerr, {}, PLANNED_AT)
        self.assertEqual((requests, known), ({}, True))


class ViewersSinceThePlanTests(unittest.TestCase):
    """Two viewers protect a series; the second one can arrive after the plan."""

    def finished(self, user_id: int, minutes_from_plan: int) -> dict:
        """A finished episode of the show's Plex item, stopped relative to the plan."""
        when = int((PLANNED_AT + timedelta(minutes=minutes_from_plan)).timestamp())
        return {"grandparent_rating_key": 5000, "user_id": user_id, "percent_complete": 96, "stopped": when}

    def run_one(self, rows):
        """Execute a candidate one person had been watching when the plan was made."""
        tautulli = FakeTautulli()
        tautulli.history_rows = rows
        one_viewer = replace(assessment(), viewing=make_viewing(viewers=1))
        return execute(
            [one_viewer],
            FakeSonarr({1: series_record()}, TAGS),
            tautulli,
            Ledger(FakeS3(), run_id="run-test", dry_run=True),
            TAGS,
            dry_run=True,
            crosswalk={5000: {"tvdb": 70001, "tmdb": 1000, "imdb": "tt0000001"}},
            planned_at=PLANNED_AT,
        )

    def test_a_second_person_finishing_an_episode_since_the_plan_stops_the_deletion(self):
        result = self.run_one([self.finished(100, -5000), self.finished(200, 5)])
        self.assertEqual(result.deleted, [])
        self.assertEqual(result.skipped[0]["why"], "an episode was finished since the plan was made")

    def test_the_viewing_the_plan_already_knew_does_not_stop_it(self):
        result = self.run_one([self.finished(100, -5000)])
        self.assertEqual([d["series_id"] for d in result.deleted], [1])


class UnplayedSeriesTests(unittest.TestCase):
    """A continuing series due only because nobody had played it is re-checked for any play.

    Its window ran from its first import. One unfinished play since the plan
    puts it back on the newest episode's clock, and no other live check sees a
    play that was neither finished nor still running.
    """

    CROSSWALK = {5000: {"tvdb": 70001, "tmdb": 1000, "imdb": "tt0000001"}}

    def play(self, percent: int, minutes_from_plan: int, show_key: int | None = 5000) -> dict:
        """One play of an episode, stopped relative to the plan."""
        when = int((PLANNED_AT + timedelta(minutes=minutes_from_plan)).timestamp())
        return {"grandparent_rating_key": show_key, "user_id": 200, "percent_complete": percent, "stopped": when}

    def candidate(self, status: str = "continuing", plays: int = 0) -> Assessment:
        """A candidate with ``plays`` plays on record when the plan was made."""
        return replace(assessment(), show=make_show(status=status), viewing=make_viewing(plays=plays))

    def run_one(self, rows, status="continuing", plays=0):
        """Execute one candidate in a dry run against these history rows."""
        tautulli = FakeTautulli()
        tautulli.history_rows = rows
        return execute(
            [self.candidate(status, plays)],
            FakeSonarr({1: series_record(status=status)}, TAGS),
            tautulli,
            Ledger(FakeS3(), run_id="run-test", dry_run=True),
            TAGS,
            dry_run=True,
            crosswalk=self.CROSSWALK,
            planned_at=PLANNED_AT,
        )

    def test_an_unfinished_play_since_the_plan_stops_the_deletion(self):
        result = self.run_one([self.play(40, 5)])
        self.assertEqual(result.deleted, [])
        self.assertIn("played since the plan was made", result.skipped[0]["why"])
        self.assertFalse(result.skipped[0]["unavailable"])

    def test_an_unfinished_play_from_before_the_plan_is_not_news(self):
        result = self.run_one([self.play(40, -5000)])
        self.assertEqual([d["series_id"] for d in result.deleted], [1])

    def test_an_ended_series_does_not_rest_on_being_unplayed(self):
        """Its window runs from its first import whoever has played it."""
        result = self.run_one([self.play(40, 5)], status="ended")
        self.assertEqual([d["series_id"] for d in result.deleted], [1])

    def test_a_series_already_played_at_the_plan_does_not_rest_on_it_either(self):
        result = self.run_one([self.play(40, 5)], plays=3)
        self.assertEqual([d["series_id"] for d in result.deleted], [1])

    def test_a_play_of_another_series_stops_nothing(self):
        crosswalk = {**self.CROSSWALK, 6000: {"tvdb": 4242, "tmdb": 4243, "imdb": "tt4242"}}
        tautulli = FakeTautulli()
        tautulli.history_rows = [self.play(40, 5, show_key=6000)]
        self.assertIsNone(volatile_block(make_show(), tautulli, None, crosswalk, PLANNED_AT, self.candidate()))

    def test_a_play_that_cannot_be_tied_to_a_show_is_a_check_that_could_not_be_made(self):
        result = self.run_one([self.play(40, 5, show_key=None)])
        self.assertEqual(result.deleted, [])
        self.assertIn("could not tell whether it was played", result.skipped[0]["why"])
        self.assertTrue(result.skipped[0]["unavailable"])

    def test_an_untied_play_does_not_stop_a_series_that_does_not_rest_on_it(self):
        result = self.run_one([self.play(40, 5, show_key=None)], status="ended")
        self.assertEqual([d["series_id"] for d in result.deleted], [1])

    def test_the_check_is_repeated_immediately_before_the_deletion(self):
        tautulli = FakeTautulli()
        tautulli.history_rows = [self.play(40, 5)]
        why = volatile_block(
            make_show(status="continuing"), tautulli, None, self.CROSSWALK, PLANNED_AT, self.candidate()
        )
        self.assertIn("played since the plan was made", why or "")

    def test_late_evidence_reports_every_show_played_since_the_plan(self):
        tautulli = FakeTautulli()
        tautulli.history_rows = [self.play(40, 5), self.play(96, 6), self.play(10, -5000)]
        _, watched, known, played = late_evidence(tautulli, None, self.CROSSWALK, PLANNED_AT)
        self.assertTrue(known)
        self.assertIn(("tvdb", "70001"), played)
        self.assertIn(("tvdb", "70001"), watched)

    def test_an_unfinished_play_alone_is_not_a_completion(self):
        tautulli = FakeTautulli()
        tautulli.history_rows = [self.play(40, 5)]
        _, watched, known, played = late_evidence(tautulli, None, self.CROSSWALK, PLANNED_AT)
        self.assertEqual((watched, known), (set(), True))
        self.assertIn(("tvdb", "70001"), played)


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
