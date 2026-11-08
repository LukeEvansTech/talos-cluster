"""Cover ported from the film engine's in-cluster review.

Every one of these fails silently in production: a stale plan still looks like
a plan, a mode mismatch still writes a ledger, and a judge asked to prove a keep
reason it has no evidence for simply guesses.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from curatortv.__main__ import count_genres, plan_refusal
from curatortv.clients import SourceError
from curatortv.executor import late_evidence, late_evidence_block
from curatortv.model import Outcome
from curatortv.planner import PlanContext, _subject_bucket, assess, fact_fingerprint, keep_tag_targets

from .fixtures import SCOPE_START, days_ago, feed_provenance, make_availability, make_show, make_viewing

NOW = datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc)


def context(**overrides) -> PlanContext:
    """A plan context with defaults for the fixed test clock."""
    base: dict[str, Any] = {
        "now": NOW,
        "scope_start": SCOPE_START,
        "coverage_start": days_ago(500),
        "history_ok": True,
    }
    base.update(overrides)
    return PlanContext(**base)


def plan(**overrides) -> dict:
    """A plan document with the fields the guard inspects."""
    base = {"mode": "act", "generated_at": (NOW - timedelta(minutes=30)).isoformat()}
    base.update(overrides)
    return base


def assessed(show) -> Outcome:
    """The outcome for one series with otherwise-default evidence."""
    return assess(show, feed_provenance(), make_availability(), make_viewing(), context()).outcome


class PlanRefusalTests(unittest.TestCase):
    """A plan has to prove it belongs to this run before anything is deleted."""

    def test_a_fresh_matching_plan_is_accepted(self):
        self.assertIsNone(plan_refusal(plan(), "act", NOW))

    def test_a_plan_from_the_other_mode_is_refused(self):
        self.assertIn("dry-run", plan_refusal(plan(mode="dry-run"), "act", NOW))

    def test_a_stale_plan_is_refused(self):
        self.assertIn("over the", plan_refusal(plan(generated_at=(NOW - timedelta(days=7)).isoformat()), "act", NOW))

    def test_a_plan_with_no_timestamp_is_refused(self):
        self.assertIn("generated_at", plan_refusal(plan(generated_at=None), "act", NOW))

    def test_an_unparseable_timestamp_is_refused(self):
        self.assertIn("generated_at", plan_refusal(plan(generated_at="last tuesday"), "act", NOW))

    def test_dry_run_is_held_to_the_same_rules(self):
        self.assertIsNone(plan_refusal(plan(mode="dry-run"), "dry-run", NOW))


class GenreCountTests(unittest.TestCase):
    """The judge must be given what its keep reasons are stated in terms of."""

    SERIES = [
        {"id": 1, "genres": ["Animation", "Comedy"], "statistics": {"episodeFileCount": 12}},
        {"id": 2, "genres": ["Animation"], "statistics": {"episodeFileCount": 3}},
        {"id": 3, "genres": ["Documentary"], "statistics": {"episodeFileCount": 1}},
        # Monitored but nothing downloaded: not something the library holds.
        {"id": 4, "genres": ["Animation"], "statistics": {"episodeFileCount": 0}},
    ]

    def test_genre_counts_exclude_series_with_no_episodes(self):
        self.assertEqual(count_genres(self.SERIES)["Animation"], 2)

    def test_genre_counts_are_library_wide(self):
        self.assertEqual(count_genres(self.SERIES)["Documentary"], 1)

    def test_series_with_no_genres_do_not_break_the_count(self):
        self.assertEqual(count_genres([{"id": 9}]), {})


class _History:  # pylint: disable=too-few-public-methods
    """History plus a metadata lookup, as the real client provides."""

    def __init__(self, rows, metadata=None, fail_metadata=False):
        self.rows = rows
        self.metadata_by_key = metadata or {}
        self.fail_metadata = fail_metadata
        self.lookups: list[int] = []

    def episode_history(self):
        """Complete history."""
        return self.rows, {"complete": True}

    def metadata(self, rating_key):
        """Resolve one key, recording that it was asked for."""
        self.lookups.append(rating_key)
        if self.fail_metadata:
            raise SourceError("tautulli refused")
        return self.metadata_by_key.get(rating_key)


def finished(show_key, stopped) -> list[dict]:
    """One completed episode row for a show, stopped at ``stopped``."""
    return [{"grandparent_rating_key": show_key, "percent_complete": 99, "stopped": stopped}]


class LateEvidenceWindowTests(unittest.TestCase):
    """Only evidence newer than the plan counts."""

    CROSSWALK = {5000: {"tvdb": 555, "tmdb": None, "imdb": "tt555"}}

    def test_a_play_from_before_the_plan_is_ignored(self):
        old = int((NOW - timedelta(days=30)).timestamp())
        _, watched, known = late_evidence(_History(finished(5000, old)), None, self.CROSSWALK, NOW)
        self.assertTrue(known)
        self.assertEqual(watched, set())

    def test_a_play_after_the_plan_is_caught(self):
        new = int((NOW + timedelta(minutes=20)).timestamp())
        _, watched, _ = late_evidence(_History(finished(5000, new)), None, self.CROSSWALK, NOW)
        self.assertEqual(watched, {("tvdb", "555"), ("imdb", "tt555")})

    def test_an_unknown_plan_time_refuses_rather_than_guesses(self):
        self.assertFalse(late_evidence(_History([]), None, self.CROSSWALK, None)[2])

    def test_a_finished_episode_with_no_show_key_refuses(self):
        new = int((NOW + timedelta(minutes=20)).timestamp())
        self.assertFalse(late_evidence(_History(finished(None, new)), None, self.CROSSWALK, NOW)[2])


class FirstViewingIsCaughtTests(unittest.TestCase):
    """The crosswalk cannot contain a show nobody had played when the plan ran."""

    AFTER = int((NOW + timedelta(minutes=20)).timestamp())

    def test_a_first_viewing_on_an_unknown_key_is_caught(self):
        history = _History(finished(9999, self.AFTER), metadata={9999: {"guids": ["tvdb://555", "imdb://tt555"]}})
        _, watched, known = late_evidence(history, None, {}, NOW)
        self.assertTrue(known)
        self.assertEqual(watched, {("tvdb", "555"), ("imdb", "tt555")})
        self.assertEqual(history.lookups, [9999])

    def test_an_unattributable_completion_stops_the_run(self):
        self.assertFalse(late_evidence(_History(finished(9999, self.AFTER), metadata={}), None, {}, NOW)[2])

    def test_a_metadata_failure_stops_the_run(self):
        self.assertFalse(late_evidence(_History(finished(9999, self.AFTER), fail_metadata=True), None, {}, NOW)[2])

    def test_old_plays_are_not_looked_up_at_all(self):
        before = int((NOW - timedelta(days=30)).timestamp())
        history = _History(finished(9999, before))
        self.assertTrue(late_evidence(history, None, {}, NOW)[2])
        self.assertEqual(history.lookups, [])


class LateEvidenceBlockTests(unittest.TestCase):
    """Two things change outside Sonarr between planning and execution."""

    def test_a_request_made_since_the_plan_blocks_the_delete(self):
        show = make_show(tvdb_id=555)
        why = late_evidence_block(show, {("tvdb", 555): {"requested_by": "Vf0rVend3tta"}}, set())
        self.assertIn("requested by Vf0rVend3tta", why)

    def test_a_request_matched_on_tmdb_blocks_the_delete(self):
        show = make_show(tvdb_id=555, tmdb_id=777)
        self.assertIsNotNone(late_evidence_block(show, {("tmdb", 777): {}}, set()))

    def test_a_finished_episode_on_any_identifier_blocks(self):
        show = make_show(tvdb_id=555, tmdb_id=777, imdb_id="tt1")
        for watched in ({("tvdb", "555")}, {("tmdb", "777")}, {("imdb", "tt1")}):
            self.assertIn("finished since the plan", late_evidence_block(show, {}, watched))

    def test_an_untouched_series_is_not_blocked(self):
        self.assertIsNone(late_evidence_block(make_show(tvdb_id=555), {}, {("tvdb", "999")}))

    def test_a_series_with_no_ids_is_not_matched_by_accident(self):
        show = make_show(tvdb_id=None, tmdb_id=None, imdb_id=None)
        self.assertIsNone(late_evidence_block(show, {("tvdb", 555): {}}, {("tvdb", "555")}))


class KeepBarIsAProtectionTests(unittest.TestCase):
    """A series about to gain permanent protection must not be deleted first."""

    def test_a_series_over_the_bar_is_protected_before_the_tag_exists(self):
        self.assertIs(assessed(make_show(imdb_votes=650_166, imdb_score=8.5)), Outcome.PROTECTED)

    def test_a_series_under_the_bar_is_unaffected(self):
        self.assertIs(assessed(make_show(imdb_votes=200_000, imdb_score=6.4)), Outcome.CANDIDATE)

    def test_candidates_and_keep_tag_targets_cannot_overlap(self):
        shows = [
            make_show(series_id=1, imdb_votes=200_000, imdb_score=7.4),
            make_show(series_id=2, imdb_votes=200_000, imdb_score=6.4),
        ]
        candidates = {s.series_id for s in shows if assessed(s) is Outcome.CANDIDATE}
        self.assertEqual(candidates & {s.series_id for s in keep_tag_targets(shows)}, set())

    def test_a_high_rated_series_marked_eligible_is_not_bar_protected(self):
        show = make_show(tags=frozenset({"kometa", "cleanup-eligible"}), imdb_votes=200_000, imdb_score=7.4)
        self.assertIs(assessed(show), Outcome.CANDIDATE)

    def test_keep_and_eligible_together_still_protect(self):
        show = make_show(tags=frozenset({"keep", "cleanup-eligible"}), imdb_votes=200_000, imdb_score=7.4)
        self.assertIs(assessed(show), Outcome.PROTECTED)


class FingerprintTests(unittest.TestCase):
    """A keep granted on a subject must reopen when the subject collapses."""

    def test_a_collapsed_subject_changes_the_fingerprint(self):
        show, prov, view = make_show(), feed_provenance(), make_viewing()
        self.assertNotEqual(
            fact_fingerprint(show, prov, view, {"Anime": 56}), fact_fingerprint(show, prov, view, {"Anime": 2})
        )

    def test_ordinary_library_growth_does_not_reopen_everything(self):
        show, prov, view = make_show(), feed_provenance(), make_viewing()
        self.assertEqual(
            fact_fingerprint(show, prov, view, {"Drama": 402}), fact_fingerprint(show, prov, view, {"Drama": 408})
        )

    def test_a_new_season_changes_the_fingerprint(self):
        prov, view = feed_provenance(), make_viewing()
        self.assertNotEqual(
            fact_fingerprint(make_show(season_count=1), prov, view),
            fact_fingerprint(make_show(season_count=2), prov, view),
        )

    def test_exact_and_bucketed_ranges_never_collide(self):
        self.assertNotEqual(_subject_bucket(56), _subject_bucket(2))
        buckets = [_subject_bucket(n) for n in range(4000)]
        self.assertFalse(any(b < 25 and buckets.count(b) > 1 for b in set(buckets)))


if __name__ == "__main__":
    unittest.main()
