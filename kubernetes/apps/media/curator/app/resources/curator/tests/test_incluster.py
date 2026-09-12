"""Cover for the defects review found in the in-cluster rework.

Every one of these fails silently in production: a stale plan still looks like
a plan, a mode mismatch still writes a ledger, and a judge asked to prove a keep
reason it has no evidence for simply deletes.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from curator.__main__ import count_genres, count_owned_siblings, plan_refusal
from curator.executor import late_evidence, late_evidence_block
from curator.model import Outcome
from curator.planner import PlanContext, assess, fact_fingerprint, keep_tag_targets

from .fixtures import SCOPE_START, days_ago, feed_provenance, make_availability, make_film, make_viewing


def context(**overrides) -> PlanContext:
    """A plan context with defaults for the fixed test clock."""
    base: dict[str, Any] = {
        "now": NOW,
        "scope_start": SCOPE_START,
        "monitored_collection_tmdb_ids": frozenset(),
        "coverage_start": days_ago(500),
        "history_ok": True,
    }
    base.update(overrides)
    return PlanContext(**base)


NOW = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)


def plan(**overrides) -> dict:
    """A plan document with the fields the guard inspects."""
    base = {"mode": "act", "generated_at": (NOW - timedelta(minutes=30)).isoformat()}
    base.update(overrides)
    return base


class PlanRefusalTests(unittest.TestCase):
    """A plan has to prove it belongs to this run before anything is deleted."""

    def test_a_fresh_matching_plan_is_accepted(self):
        self.assertIsNone(plan_refusal(plan(), "act", NOW))

    def test_a_plan_from_the_other_mode_is_refused(self):
        """Rehearsal decisions would otherwise suppress the first real run."""
        why = plan_refusal(plan(mode="dry-run"), "act", NOW)
        self.assertIn("dry-run", why)

    def test_a_stale_plan_is_refused(self):
        """If the planner failed, last week's plan is still sitting there.

        Re-validation refreshes tags and playback but not play history, so a
        film watched since would not be protected by it.
        """
        why = plan_refusal(plan(generated_at=(NOW - timedelta(days=7)).isoformat()), "act", NOW)
        self.assertIn("over the", why)

    def test_a_plan_with_no_timestamp_is_refused(self):
        self.assertIn("generated_at", plan_refusal(plan(generated_at=None), "act", NOW))

    def test_an_unparseable_timestamp_is_refused(self):
        self.assertIn("generated_at", plan_refusal(plan(generated_at="last tuesday"), "act", NOW))

    def test_dry_run_is_held_to_the_same_rules(self):
        self.assertIsNone(plan_refusal(plan(mode="dry-run"), "dry-run", NOW))


class KeepEvidenceTests(unittest.TestCase):
    """The judge must be given what its keep reasons are stated in terms of."""

    MOVIES = [
        {"id": 1, "tmdbId": 100, "genres": ["Animation", "Family"], "hasFile": True},
        {"id": 2, "tmdbId": 200, "genres": ["Animation"], "hasFile": True},
        {"id": 3, "tmdbId": 300, "genres": ["Documentary"], "hasFile": True},
        # Monitored but not yet downloaded: a record the planner itself calls
        # MISSING_FILE, so it is not something the library holds.
        {"id": 4, "tmdbId": 400, "genres": ["Animation"], "hasFile": False},
    ]
    COLLECTIONS = [
        {
            "title": "Cars Collection",
            "movies": [{"tmdbId": 100}, {"tmdbId": 200}, {"tmdbId": 999}],
        },
        {
            "title": "Awaited Collection",
            "movies": [{"tmdbId": 300}, {"tmdbId": 400}],
        },
    ]

    def test_siblings_counts_other_owned_entries_only(self):
        counts = count_owned_siblings(self.MOVIES, self.COLLECTIONS)
        self.assertEqual(counts[1], 1)
        self.assertEqual(counts[2], 1)

    def test_an_unowned_collection_entry_is_not_counted(self):
        """tmdbId 999 is in the collection but not in the library."""
        self.assertEqual(count_owned_siblings(self.MOVIES, self.COLLECTIONS)[1], 1)

    def test_a_sibling_with_no_file_is_not_owned(self):
        """A film Radarr is still looking for is not collection support.

        The prompt offers siblings_owned as proof the library holds part of a
        set, so counting a record with no file would make that proof false.
        """
        self.assertEqual(count_owned_siblings(self.MOVIES, self.COLLECTIONS).get(3, 0), 0)

    def test_genre_counts_exclude_films_with_no_file(self):
        """Two Animation records are on disk; the third is still missing."""
        self.assertEqual(count_genres(self.MOVIES)["Animation"], 2)

    def test_genre_counts_are_library_wide(self):
        counts = count_genres(self.MOVIES)
        self.assertEqual(counts["Animation"], 2)
        self.assertEqual(counts["Documentary"], 1)

    def test_films_with_no_genres_do_not_break_the_count(self):
        self.assertEqual(count_genres([{"id": 9}]), {})


if __name__ == "__main__":
    unittest.main()


class LateEvidenceWindowTests(unittest.TestCase):
    """Only evidence newer than the plan counts.

    A film with one completion reaches the candidate list by design: the window
    already gave the household its chance, and a single play is explicitly not
    protection. Treating that pre-existing play as new would overturn that rule
    without anyone deciding to.
    """

    class _Tautulli:  # pylint: disable=too-few-public-methods
        """History double returning rows at fixed timestamps."""

        def __init__(self, rows):
            self.rows = rows

        def movie_history(self):
            """Rows plus a complete-retrieval marker."""
            return self.rows, {"complete": True}

    CROSSWALK = {5000: {"tmdb": 555, "imdb": "tt555"}}

    def _rows(self, stopped: int):
        return [{"rating_key": 5000, "percent_complete": 99, "stopped": stopped}]

    def test_a_play_from_before_the_plan_is_ignored(self):
        planned = NOW
        old_play = int((NOW - timedelta(days=30)).timestamp())
        _, watched, known = late_evidence(self._Tautulli(self._rows(old_play)), None, self.CROSSWALK, planned)
        self.assertTrue(known)
        self.assertEqual(watched, set())

    def test_a_play_after_the_plan_is_caught(self):
        new_play = int((NOW + timedelta(minutes=20)).timestamp())
        _, watched, _ = late_evidence(self._Tautulli(self._rows(new_play)), None, self.CROSSWALK, NOW)
        self.assertEqual(watched, {555})

    def test_an_unknown_plan_time_refuses_rather_than_guesses(self):
        _, _, known = late_evidence(self._Tautulli([]), None, self.CROSSWALK, None)
        self.assertFalse(known)


class LateEvidenceTests(unittest.TestCase):
    """Two things change outside Radarr between planning and execution.

    Live re-validation covers everything Radarr holds. It does not cover
    somebody requesting a film, or somebody finishing one -- and a plan is at
    least half an hour old by the time it is acted on.
    """

    def test_a_request_made_since_the_plan_blocks_the_delete(self):
        film = make_film(tmdb_id=555)
        why = late_evidence_block(film, {555: {"requested_by": "Vf0rVend3tta"}}, set())
        self.assertIn("requested by Vf0rVend3tta", why)

    def test_a_completion_since_the_plan_blocks_the_delete(self):
        film = make_film(tmdb_id=555)
        self.assertIn("completed by someone", late_evidence_block(film, {}, {555}))

    def test_an_untouched_film_is_not_blocked(self):
        self.assertIsNone(late_evidence_block(make_film(tmdb_id=555), {}, {999}))

    def test_a_film_with_no_tmdb_id_is_not_matched_by_accident(self):
        self.assertIsNone(late_evidence_block(make_film(tmdb_id=None), {555: {}}, {555}))


class KeepBarIsAProtectionTests(unittest.TestCase):
    """A film about to gain permanent protection must not be deleted first.

    Assessment runs before tag maintenance, so a film qualifying for the
    allow-list bar could previously appear in both keep_tag_additions and
    candidates in the same run.
    """

    def test_a_film_over_the_bar_is_protected_before_the_tag_exists(self):
        film = make_film(tags=frozenset({"src-tmdb-popular"}), imdb_votes=200_000, imdb_score=7.4)
        result = assess(film, feed_provenance(), make_availability(), make_viewing(), context())
        self.assertIs(result.outcome, Outcome.PROTECTED)
        self.assertIn("allow-list bar", result.protections[0])

    def test_a_film_under_the_bar_is_unaffected(self):
        film = make_film(tags=frozenset({"src-tmdb-popular"}), imdb_votes=200_000, imdb_score=6.4)
        result = assess(film, feed_provenance(), make_availability(), make_viewing(), context())
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_candidates_and_keep_tag_targets_cannot_overlap(self):
        films = [
            make_film(movie_id=1, tags=frozenset({"src-x"}), imdb_votes=200_000, imdb_score=7.4),
            make_film(movie_id=2, tags=frozenset({"src-x"}), imdb_votes=200_000, imdb_score=6.4),
        ]
        candidates = {
            f.movie_id
            for f in films
            if assess(f, feed_provenance(), make_availability(), make_viewing(), context()).outcome is Outcome.CANDIDATE
        }
        targets = {f.movie_id for f in keep_tag_targets(films)}
        self.assertEqual(candidates & targets, set())


class FingerprintCoversKeepEvidenceTests(unittest.TestCase):
    """A keep granted on collection support must reopen when it disappears."""

    def test_losing_the_last_owned_sibling_changes_the_fingerprint(self):
        film, prov, view = make_film(), feed_provenance(), make_viewing()
        with_sibling = fact_fingerprint(film, prov, view, False, siblings_owned=1)
        without = fact_fingerprint(film, prov, view, False, siblings_owned=0)
        self.assertNotEqual(with_sibling, without)

    def test_a_collapsed_subject_changes_the_fingerprint(self):
        film, prov, view = make_film(), feed_provenance(), make_viewing()
        many = fact_fingerprint(film, prov, view, False, subject_counts={"Football": 56})
        few = fact_fingerprint(film, prov, view, False, subject_counts={"Football": 2})
        self.assertNotEqual(many, few)

    def test_ordinary_library_growth_does_not_reopen_everything(self):
        """Counts are bucketed, so a handful of new films is not a changed fact."""
        film, prov, view = make_film(), feed_provenance(), make_viewing()
        before = fact_fingerprint(film, prov, view, False, subject_counts={"Action": 1002})
        after = fact_fingerprint(film, prov, view, False, subject_counts={"Action": 1008})
        self.assertEqual(before, after)
