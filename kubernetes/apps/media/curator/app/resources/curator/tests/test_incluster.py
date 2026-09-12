"""Cover for the defects review found in the in-cluster rework.

Every one of these fails silently in production: a stale plan still looks like
a plan, a mode mismatch still writes a ledger, and a judge asked to prove a keep
reason it has no evidence for simply deletes.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from curator.__main__ import count_genres, count_owned_siblings, plan_refusal

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
        {"id": 1, "tmdbId": 100, "genres": ["Animation", "Family"]},
        {"id": 2, "tmdbId": 200, "genres": ["Animation"]},
        {"id": 3, "tmdbId": 300, "genres": ["Documentary"]},
    ]
    COLLECTIONS = [
        {
            "title": "Cars Collection",
            "movies": [{"tmdbId": 100}, {"tmdbId": 200}, {"tmdbId": 999}],
        }
    ]

    def test_siblings_counts_other_owned_entries_only(self):
        counts = count_owned_siblings(self.MOVIES, self.COLLECTIONS)
        self.assertEqual(counts[1], 1)
        self.assertEqual(counts[2], 1)

    def test_an_unowned_collection_entry_is_not_counted(self):
        """tmdbId 999 is in the collection but not in the library."""
        self.assertEqual(count_owned_siblings(self.MOVIES, self.COLLECTIONS)[1], 1)

    def test_a_film_in_no_collection_has_no_siblings(self):
        self.assertEqual(count_owned_siblings(self.MOVIES, self.COLLECTIONS).get(3, 0), 0)

    def test_genre_counts_are_library_wide(self):
        counts = count_genres(self.MOVIES)
        self.assertEqual(counts["Animation"], 2)
        self.assertEqual(counts["Documentary"], 1)

    def test_films_with_no_genres_do_not_break_the_count(self):
        self.assertEqual(count_genres([{"id": 9}]), {})


if __name__ == "__main__":
    unittest.main()
