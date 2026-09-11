"""Provenance, availability and viewing evidence."""

from __future__ import annotations

import unittest

from curator.evidence import (
    build_crosswalk,
    first_import_from_history,
    resolve_availability,
    resolve_provenance,
    resolve_viewing,
    window_days,
)
from curator.model import HistoryStatus, Origin

from .fixtures import (
    NOW,
    days_ago,
    import_event,
    make_availability,
    make_film,
    play_row,
)


class ProvenanceTests(unittest.TestCase):
    """Section 1: how a film got here, and what counts as evidence."""

    def test_request_record_makes_it_requested(self):
        film = make_film(tmdb_id=42, tags=frozenset())
        requests = {42: {"requested_by": "Vf0rVend3tta", "created_at": "2026-05-16T00:00:00Z"}}
        result = resolve_provenance(film, requests)
        self.assertIs(result.origin, Origin.REQUESTED)
        self.assertEqual(result.requested_by, "Vf0rVend3tta")

    def test_request_wins_even_when_a_feed_also_lists_it(self):
        """A feed offering a title does not un-ask the person who asked for it."""
        film = make_film(tmdb_id=42, tags=frozenset({"src-tmdb-popular"}))
        result = resolve_provenance(film, {42: {"requested_by": "Luke"}})
        self.assertIs(result.origin, Origin.REQUESTED)

    def test_provenance_tag_makes_it_feed(self):
        film = make_film(tmdb_id=7, tags=frozenset({"src-imdb-top-250"}))
        result = resolve_provenance(film, {})
        self.assertIs(result.origin, Origin.FEED)
        self.assertIn("src-imdb-top-250", result.evidence[0])

    def test_no_evidence_is_unknown_not_feed(self):
        """The date a record was created says nothing about who created it."""
        film = make_film(tmdb_id=9, tags=frozenset(), added=days_ago(200))
        result = resolve_provenance(film, {})
        self.assertIs(result.origin, Origin.UNKNOWN)

    def test_manual_addition_after_scope_start_is_not_feed(self):
        film = make_film(tmdb_id=11, tags=frozenset({"anime"}), added=days_ago(100))
        self.assertIs(resolve_provenance(film, {}).origin, Origin.UNKNOWN)


class AvailabilityTests(unittest.TestCase):
    """Section 2: when the film actually became watchable."""

    def test_first_import_wins_over_a_later_upgrade(self):
        history = [
            import_event(days_ago(200)),
            import_event(days_ago(10)),
            {"eventType": "grabbed", "date": days_ago(400).isoformat()},
        ]
        self.assertEqual(first_import_from_history(history), days_ago(200))

    def test_upgrade_does_not_restart_the_window(self):
        film = make_film(added=days_ago(365))
        history = [import_event(days_ago(300)), import_event(days_ago(1))]
        result = resolve_availability(film, history, None, None, NOW)
        self.assertEqual(result.first_playable, days_ago(300))
        self.assertEqual(result.source, "radarr-history")

    def test_old_record_with_a_file_that_arrived_yesterday(self):
        """A request placed pre-release gets its full window from the import."""
        film = make_film(added=days_ago(400))
        result = resolve_availability(film, [import_event(days_ago(1))], None, None, NOW)
        self.assertEqual(result.first_playable, days_ago(1))

    def test_missing_file_has_no_availability(self):
        film = make_film(has_file=False)
        result = resolve_availability(film, [], None, None, NOW)
        self.assertIsNone(result.first_playable)
        self.assertEqual(result.source, "none")

    def test_aged_out_history_falls_back_to_the_ledger(self):
        film = make_film(added=days_ago(900))
        result = resolve_availability(film, [], days_ago(500), days_ago(300), NOW)
        self.assertEqual(result.first_playable, days_ago(300))
        self.assertEqual(result.source, "ledger")

    def test_no_history_and_no_ledger_starts_the_clock_now(self):
        """An unknown start must postpone the window, never shorten it."""
        film = make_film(added=days_ago(900))
        result = resolve_availability(film, [], days_ago(500), None, NOW)
        self.assertEqual(result.first_playable, NOW)
        self.assertEqual(result.source, "ledger-bootstrap")

    def test_window_lengths(self):
        self.assertEqual(window_days(make_film(imdb_score=4.9)), 30)
        self.assertEqual(window_days(make_film(imdb_score=6.9)), 90)
        self.assertEqual(window_days(make_film(imdb_score=None)), 90)


class ViewingTests(unittest.TestCase):
    """Section 3: complete, correctly matched play evidence."""

    def setUp(self):
        self.crosswalk = build_crosswalk(
            {
                5000: {
                    "guids": ["tmdb://1000", "imdb://tt0000001"],
                    "title": "Test Film",
                    "year": 2026,
                },
                6000: {
                    "guids": ["tmdb://2000", "imdb://tt0000002"],
                    "title": "Other",
                    "year": 2001,
                },
            }
        )

    def test_crosswalk_extracts_external_ids(self):
        self.assertEqual(self.crosswalk[5000]["tmdb"], 1000)
        self.assertEqual(self.crosswalk[5000]["imdb"], "tt0000001")

    def test_two_users_finishing_counts_two(self):
        rows = {5000: [play_row(5000, 11, 95, 1), play_row(5000, 12, 99, 2)]}
        result = resolve_viewing(make_film(), rows, self.crosswalk, days_ago(400), make_availability())
        self.assertEqual(result.distinct_completers, 2)
        self.assertIs(result.status, HistoryStatus.OK)

    def test_one_user_playing_twice_counts_once(self):
        """Distinct *users*, not rows -- a rewatch is not a second opinion."""
        rows = {
            5000: [
                play_row(5000, 11, 95, 1),
                play_row(5000, 11, 99, 2),
                play_row(5000, 11, 91, 3),
            ]
        }
        result = resolve_viewing(make_film(), rows, self.crosswalk, days_ago(400), make_availability())
        self.assertEqual(result.distinct_completers, 1)
        self.assertEqual(result.play_count, 3)

    def test_partial_plays_are_not_completions(self):
        rows = {5000: [play_row(5000, 11, 40, 1), play_row(5000, 12, 84, 2)]}
        result = resolve_viewing(make_film(), rows, self.crosswalk, days_ago(400), make_availability())
        self.assertEqual(result.distinct_completers, 0)

    def test_abandoned_plays_are_reported(self):
        rows = {5000: [play_row(5000, 11, 8, 1)]}
        result = resolve_viewing(make_film(), rows, self.crosswalk, days_ago(400), make_availability())
        self.assertTrue(any("abandoned" in note for note in result.notes))

    def test_identity_falls_back_to_imdb_then_title(self):
        crosswalk = {
            7000: {
                "tmdb": None,
                "imdb": "tt0000001",
                "title": "Test Film",
                "year": 2026,
            }
        }
        result = resolve_viewing(make_film(), {}, crosswalk, days_ago(400), make_availability())
        self.assertEqual(result.identity_via, "imdb")

    def test_absence_from_a_fully_resolved_history_is_a_verified_zero(self):
        """The films worth deleting are exactly the ones history never mentions."""
        crosswalk = {8000: {"tmdb": 999, "imdb": "tt9", "title": "Something Else", "year": 2001}}
        film = make_film(tmdb_id=1234, imdb_id="tt5555", title="Never Watched", year=2026)
        result = resolve_viewing(film, {}, crosswalk, days_ago(400), make_availability())
        self.assertIs(result.status, HistoryStatus.OK)
        self.assertTrue(result.trustworthy)
        self.assertEqual(result.distinct_completers, 0)
        self.assertEqual(result.identity_via, "no-plays")

    def test_plays_on_a_retired_plex_item_taint_the_zero(self):
        """get_metadata 404s on a reissued key, but those plays were still real."""
        film = make_film(tmdb_id=1234, imdb_id="tt5555", title="Spirited Away", year=2001)
        unattributed = [
            {
                "title": "Spirited Away",
                "year": 2001,
                "user_id": 11,
                "percent_complete": 99,
            }
        ]
        result = resolve_viewing(
            film,
            {},
            {},
            days_ago(400),
            make_availability(),
            unattributed_rows=unattributed,
        )
        self.assertIs(result.status, HistoryStatus.UNRESOLVED)
        self.assertFalse(result.trustworthy)
        self.assertIn("retired Plex item", result.notes[0])

    def test_an_unattributed_play_for_a_different_film_does_not_taint(self):
        film = make_film(title="Never Watched", year=2026)
        unattributed = [{"title": "Spirited Away", "year": 2001}]
        result = resolve_viewing(
            film,
            {},
            {},
            days_ago(400),
            make_availability(),
            unattributed_rows=unattributed,
        )
        self.assertIs(result.status, HistoryStatus.OK)

    def test_alternate_title_under_a_resolved_key_still_matches_by_id(self):
        """A Japanese-titled Plex item still carries the same tmdb id."""
        crosswalk = {
            9000: {
                "tmdb": 1000,
                "imdb": "tt0000001",
                "title": "Sen to Chihiro",
                "year": 2001,
            }
        }
        rows = {9000: [play_row(9000, 11, 97, 1), play_row(9000, 12, 96, 2)]}
        result = resolve_viewing(make_film(), rows, crosswalk, days_ago(400), make_availability())
        self.assertEqual(result.distinct_completers, 2)
        self.assertEqual(result.identity_via, "tmdb")

    def test_remake_sharing_a_title_is_ambiguous(self):
        crosswalk = {
            1: {"tmdb": 111, "imdb": "tt111", "title": "The Thing", "year": 2026},
            2: {"tmdb": 222, "imdb": "tt222", "title": "The Thing", "year": 2025},
        }
        film = make_film(tmdb_id=None, imdb_id=None, title="The Thing", year=2026)
        result = resolve_viewing(film, {}, crosswalk, days_ago(400), make_availability())
        self.assertIs(result.status, HistoryStatus.AMBIGUOUS)
        self.assertFalse(result.trustworthy)

    def test_history_starting_after_the_film_is_incomplete(self):
        """Older completions may exist outside the retained window."""
        result = resolve_viewing(make_film(), {}, self.crosswalk, days_ago(100), make_availability(days=300))
        self.assertIs(result.status, HistoryStatus.INCOMPLETE)
        self.assertFalse(result.trustworthy)

    def test_unavailable_history_is_its_own_state(self):
        result = resolve_viewing(
            make_film(),
            {},
            self.crosswalk,
            days_ago(400),
            make_availability(),
            history_ok=False,
        )
        self.assertIs(result.status, HistoryStatus.UNAVAILABLE)
        self.assertEqual(result.distinct_completers, 0)
        self.assertFalse(result.trustworthy)

    def test_historical_rating_key_still_counts(self):
        """Plex reissues keys; the old one is what the old rows carry."""
        crosswalk = dict(self.crosswalk)
        crosswalk[4000] = {
            "tmdb": 1000,
            "imdb": "tt0000001",
            "title": "Test Film",
            "year": 2026,
        }
        rows = {4000: [play_row(4000, 11, 95, 1)], 5000: [play_row(5000, 12, 97, 2)]}
        result = resolve_viewing(make_film(), rows, crosswalk, days_ago(400), make_availability())
        self.assertEqual(result.distinct_completers, 2)


if __name__ == "__main__":
    unittest.main()
