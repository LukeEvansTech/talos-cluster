"""Allow-list maintenance must not overturn a human decision."""

from __future__ import annotations

import unittest

from radarr_cleanup.planner import keep_tag_targets

from .fixtures import make_film


class KeepTagTests(unittest.TestCase):
    """Add-only, and never on top of somebody's decision."""

    def test_film_over_the_bar_is_tagged(self):
        film = make_film(tags=frozenset(), imdb_votes=6000, imdb_score=7.0)
        self.assertEqual(keep_tag_targets([film]), [film])

    def test_thin_votes_do_not_qualify(self):
        film = make_film(tags=frozenset(), imdb_votes=1200, imdb_score=8.1)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_low_score_does_not_qualify(self):
        film = make_film(tags=frozenset(), imdb_votes=90000, imdb_score=6.4)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_missing_rating_does_not_qualify(self):
        film = make_film(tags=frozenset(), imdb_votes=None, imdb_score=None)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_already_tagged_is_not_retagged(self):
        film = make_film(tags={"keep"}, imdb_votes=6000, imdb_score=7.0)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_keep_review_item_is_never_promoted(self):
        """The queue exists so a person decides; tagging it decides for them."""
        film = make_film(tags={"keep-review"}, imdb_votes=200000, imdb_score=8.4)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_human_eligibility_is_not_silently_reversed(self):
        film = make_film(tags={"cleanup-eligible"}, imdb_votes=200000, imdb_score=8.4)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_human_dismissal_is_not_silently_reversed(self):
        film = make_film(tags={"cleanup-dismissed"}, imdb_votes=200000, imdb_score=8.4)
        self.assertEqual(keep_tag_targets([film]), [])


if __name__ == "__main__":
    unittest.main()
