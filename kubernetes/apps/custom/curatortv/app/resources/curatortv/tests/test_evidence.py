"""Provenance, availability and viewing evidence."""

from __future__ import annotations

import unittest

from curatortv.evidence import (
    build_crosswalk,
    first_import_per_episode,
    resolve_availability,
    resolve_provenance,
    resolve_viewing,
    window_days,
)
from curatortv.model import HistoryStatus, Origin

from .fixtures import (
    NOW,
    days_ago,
    import_event,
    make_availability,
    make_show,
    play_row,
)


class ProvenanceTests(unittest.TestCase):
    """How a series got here, and what counts as evidence."""

    def test_request_record_makes_it_requested(self):
        show = make_show(tvdb_id=42, tags=frozenset())
        requests = {("tvdb", 42): {"requested_by": "Vf0rVend3tta", "created_at": "2026-05-16T00:00:00Z"}}
        result = resolve_provenance(show, requests)
        self.assertIs(result.origin, Origin.REQUESTED)
        self.assertEqual(result.requested_by, "Vf0rVend3tta")

    def test_request_falls_back_to_tmdb(self):
        """Seerr only records a TVDB id when it could resolve one."""
        show = make_show(tvdb_id=42, tmdb_id=99, tags=frozenset())
        self.assertIs(resolve_provenance(show, {("tmdb", 99): {"requested_by": "Luke"}}).origin, Origin.REQUESTED)

    def test_request_wins_even_when_a_feed_also_tagged_it(self):
        """A feed offering a title does not un-ask the person who asked for it."""
        show = make_show(tvdb_id=42, tags=frozenset({"kometa"}))
        self.assertIs(resolve_provenance(show, {("tvdb", 42): {"requested_by": "Luke"}}).origin, Origin.REQUESTED)

    def test_feed_tag_makes_it_feed(self):
        show = make_show(tags=frozenset({"kometa", "kometa-trending"}))
        result = resolve_provenance(show, {})
        self.assertIs(result.origin, Origin.FEED)
        self.assertIn("kometa", result.evidence[0])

    def test_feed_tags_are_configurable(self):
        show = make_show(tags=frozenset({"kometa-top250"}))
        self.assertIs(resolve_provenance(show, {}).origin, Origin.UNKNOWN)
        self.assertIs(resolve_provenance(show, {}, {"kometa-top250"}).origin, Origin.FEED)

    def test_no_evidence_is_unknown_not_feed(self):
        """The date a record was created says nothing about who created it."""
        show = make_show(tags=frozenset(), added=days_ago(190))
        self.assertIs(resolve_provenance(show, {}).origin, Origin.UNKNOWN)

    def test_an_unrelated_tag_is_not_feed(self):
        self.assertIs(resolve_provenance(make_show(tags=frozenset({"anime"})), {}).origin, Origin.UNKNOWN)


class AvailabilityTests(unittest.TestCase):
    """When the series actually became watchable, and when its window started."""

    def test_each_episode_keeps_its_first_import(self):
        history = [
            import_event(days_ago(200), episode_id=1),
            import_event(days_ago(10), episode_id=1),
            import_event(days_ago(150), episode_id=2),
            {"eventType": "grabbed", "date": days_ago(400).isoformat(), "episodeId": 3},
        ]
        firsts = first_import_per_episode(history)
        self.assertEqual(firsts, {1: days_ago(200), 2: days_ago(150)})

    def test_an_upgrade_does_not_restart_an_ended_show(self):
        show = make_show(status="ended")
        history = [import_event(days_ago(300), 1), import_event(days_ago(250), 2), import_event(days_ago(1), 2)]
        result = resolve_availability(show, history, None, None, NOW)
        self.assertEqual(result.first_playable, days_ago(300))
        self.assertEqual(result.clock, days_ago(300))
        self.assertEqual(result.source, "sonarr-history")

    def test_an_upgrade_does_not_restart_a_continuing_show(self):
        show = make_show(status="continuing")
        history = [import_event(days_ago(300), 1), import_event(days_ago(250), 2), import_event(days_ago(1), 1)]
        self.assertEqual(resolve_availability(show, history, None, None, NOW).clock, days_ago(250))

    def test_a_new_episode_restarts_a_continuing_show(self):
        show = make_show(status="continuing")
        history = [import_event(days_ago(300), 1), import_event(days_ago(20), 2)]
        result = resolve_availability(show, history, None, None, NOW)
        self.assertEqual(result.first_playable, days_ago(300))
        self.assertEqual(result.clock, days_ago(20))

    def test_an_ended_show_runs_from_its_first_import(self):
        show = make_show(status="ended")
        history = [import_event(days_ago(300), 1), import_event(days_ago(20), 2)]
        self.assertEqual(resolve_availability(show, history, None, None, NOW).clock, days_ago(300))

    def test_old_record_with_files_that_arrived_yesterday(self):
        show = make_show(added=days_ago(400))
        result = resolve_availability(show, [import_event(days_ago(1))], None, None, NOW)
        self.assertEqual(result.clock, days_ago(1))

    def test_missing_files_have_no_availability(self):
        result = resolve_availability(make_show(has_file=False), [], None, None, NOW)
        self.assertIsNone(result.first_playable)
        self.assertEqual(result.source, "none")

    def test_aged_out_history_falls_back_to_the_ledger(self):
        result = resolve_availability(make_show(added=days_ago(900)), [], days_ago(500), days_ago(300), NOW)
        self.assertEqual(result.first_playable, days_ago(300))
        self.assertEqual(result.source, "ledger")

    def test_ledger_older_than_retained_imports_wins_for_an_ended_show(self):
        history = [import_event(days_ago(2))]
        result = resolve_availability(make_show(status="ended"), history, days_ago(30), days_ago(300), NOW)
        self.assertEqual(result.clock, days_ago(300))
        self.assertEqual(result.source, "ledger")

    def test_a_newer_ledger_entry_does_not_override_history(self):
        history = [import_event(days_ago(200))]
        result = resolve_availability(make_show(), history, days_ago(400), days_ago(100), NOW)
        self.assertEqual(result.first_playable, days_ago(200))

    def test_no_history_and_no_ledger_starts_the_clock_now(self):
        """An unknown start must postpone the window, never shorten it."""
        result = resolve_availability(make_show(added=days_ago(900)), [], days_ago(500), None, NOW)
        self.assertEqual(result.first_playable, NOW)
        self.assertEqual(result.source, "ledger-bootstrap")

    def test_window_lengths(self):
        self.assertEqual(window_days(make_show(imdb_score=5.4)), 60)
        self.assertEqual(window_days(make_show(imdb_score=5.5)), 180)
        self.assertEqual(window_days(make_show(imdb_score=None)), 180)


class ViewingTests(unittest.TestCase):
    """Complete, correctly matched play evidence, counted in episodes."""

    def setUp(self):
        self.crosswalk = build_crosswalk(
            {
                5000: {
                    "guids": ["imdb://tt0000001", "tmdb://1000", "tvdb://70001"],
                    "title": "Test Show",
                    "year": 2026,
                },
                6000: {"guids": ["imdb://tt0000002", "tmdb://2000", "tvdb://70002"], "title": "Other", "year": 2001},
            }
        )

    def view(self, rows, show=None, **kwargs):
        """Resolve viewing for the default show against the default crosswalk."""
        return resolve_viewing(
            show or make_show(),
            rows,
            kwargs.pop("crosswalk", self.crosswalk),
            kwargs.pop("coverage", days_ago(400)),
            kwargs.pop("availability", make_availability()),
            **kwargs,
        )

    def test_crosswalk_extracts_external_ids(self):
        self.assertEqual(self.crosswalk[5000]["tvdb"], 70001)
        self.assertEqual(self.crosswalk[5000]["tmdb"], 1000)
        self.assertEqual(self.crosswalk[5000]["imdb"], "tt0000001")

    def test_two_users_finishing_episodes_counts_two_viewers(self):
        rows = {5000: [play_row(5000, 11, 95, 1), play_row(5000, 12, 99, 2, episode=2)]}
        result = self.view(rows)
        self.assertEqual(result.distinct_viewers, 2)
        self.assertIs(result.status, HistoryStatus.OK)

    def test_rewatching_one_episode_counts_once(self):
        rows = {5000: [play_row(5000, 11, 95, 1), play_row(5000, 11, 99, 2), play_row(5000, 11, 91, 3)]}
        result = self.view(rows)
        self.assertEqual(result.distinct_viewers, 1)
        self.assertEqual(result.episodes_completed, 1)
        self.assertEqual(result.play_count, 3)

    def test_episodes_completed_is_the_most_any_one_person_finished(self):
        rows = {
            5000: [play_row(5000, 11, 95, i, episode=i) for i in range(1, 6)]
            + [play_row(5000, 12, 95, 10 + i, episode=i) for i in range(1, 3)]
        }
        result = self.view(rows)
        self.assertEqual(result.episodes_completed, 5)
        self.assertEqual(result.distinct_viewers, 2)

    def test_same_episode_number_in_another_season_is_distinct(self):
        rows = {5000: [play_row(5000, 11, 95, 1, season=1), play_row(5000, 11, 95, 2, season=2)]}
        self.assertEqual(self.view(rows).episodes_completed, 2)

    def test_partial_plays_are_not_completions(self):
        rows = {5000: [play_row(5000, 11, 40, 1), play_row(5000, 12, 84, 2)]}
        result = self.view(rows)
        self.assertEqual(result.distinct_viewers, 0)
        self.assertEqual(result.play_count, 2)

    def test_last_played_is_the_newest_row(self):
        rows = {5000: [play_row(5000, 11, 40, 1, date=1_780_000_000), play_row(5000, 11, 40, 2, date=1_790_000_000)]}
        self.assertEqual(int(self.view(rows).last_played.timestamp()), 1_790_000_000)

    def test_abandoned_plays_are_reported(self):
        rows = {5000: [play_row(5000, 11, 8, 1)]}
        self.assertTrue(any("abandoned" in note for note in self.view(rows).notes))

    def test_identity_prefers_tvdb_then_tmdb_then_imdb(self):
        only_tmdb = {7000: {"tvdb": None, "tmdb": 1000, "imdb": None, "title": "x", "year": 2026}}
        only_imdb = {7000: {"tvdb": None, "tmdb": None, "imdb": "tt0000001", "title": "x", "year": 2026}}
        self.assertEqual(self.view({7000: [play_row(7000, 1, 9, 1)]}, crosswalk=only_tmdb).identity_via, "tmdb")
        self.assertEqual(self.view({7000: [play_row(7000, 1, 9, 1)]}, crosswalk=only_imdb).identity_via, "imdb")
        self.assertEqual(self.view({5000: [play_row(5000, 1, 9, 1)]}).identity_via, "tvdb")

    def test_absence_from_a_fully_resolved_history_is_a_verified_zero(self):
        """The series worth deleting are exactly the ones history never mentions."""
        show = make_show(tvdb_id=1234, tmdb_id=4321, imdb_id="tt5555", title="Never Watched")
        result = self.view({}, show=show)
        self.assertIs(result.status, HistoryStatus.OK)
        self.assertTrue(result.trustworthy)
        self.assertEqual(result.distinct_viewers, 0)
        self.assertEqual(result.identity_via, "no-plays")

    def test_plays_on_a_retired_plex_item_taint_the_zero(self):
        show = make_show(tvdb_id=1234, tmdb_id=None, imdb_id=None, title="Vikings", year=2013)
        unattributed = [{"grandparent_title": "Vikings", "year": 2013, "user_id": 11, "percent_complete": 99}]
        result = self.view({}, show=show, crosswalk={}, unattributed_rows=unattributed)
        self.assertIs(result.status, HistoryStatus.UNRESOLVED)
        self.assertFalse(result.trustworthy)
        self.assertIn("retired Plex item", result.notes[0])

    def test_an_unattributed_play_for_another_series_does_not_taint(self):
        show = make_show(title="Never Watched")
        unattributed = [{"grandparent_title": "Vikings", "year": 2013}]
        self.assertIs(self.view({}, show=show, crosswalk={}, unattributed_rows=unattributed).status, HistoryStatus.OK)

    def test_alternate_title_under_a_resolved_key_still_matches_by_id(self):
        crosswalk = {9000: {"tvdb": 70001, "tmdb": None, "imdb": None, "title": "Kaiju No. 8", "year": 2024}}
        rows = {9000: [play_row(9000, 11, 97, 1), play_row(9000, 12, 96, 2)]}
        result = self.view(rows, crosswalk=crosswalk)
        self.assertEqual(result.distinct_viewers, 2)
        self.assertEqual(result.identity_via, "tvdb")

    def test_a_reboot_sharing_a_title_is_ambiguous(self):
        crosswalk = {
            1: {"tvdb": 111, "tmdb": None, "imdb": None, "title": "Doctor Who", "year": 2026},
            2: {"tvdb": 222, "tmdb": None, "imdb": None, "title": "Doctor Who", "year": 2025},
        }
        show = make_show(tvdb_id=None, tmdb_id=None, imdb_id=None, title="Doctor Who", year=2026)
        result = self.view({}, show=show, crosswalk=crosswalk)
        self.assertIs(result.status, HistoryStatus.AMBIGUOUS)
        self.assertFalse(result.trustworthy)

    def test_history_starting_after_the_series_is_incomplete(self):
        """Measured from the first import, not the window: old seasons were watchable too."""
        availability = make_availability(days=300, window_days=10)
        result = self.view({}, coverage=days_ago(100), availability=availability)
        self.assertIs(result.status, HistoryStatus.INCOMPLETE)

    def test_unbounded_coverage_is_incomplete(self):
        self.assertIs(self.view({}, coverage=None).status, HistoryStatus.INCOMPLETE)

    def test_unavailable_history_is_its_own_state(self):
        result = self.view({}, history_ok=False)
        self.assertIs(result.status, HistoryStatus.UNAVAILABLE)
        self.assertFalse(result.trustworthy)

    def test_historical_rating_key_still_counts(self):
        """Plex reissues keys; the old one is what the old rows carry."""
        crosswalk = dict(self.crosswalk)
        crosswalk[4000] = {"tvdb": 70001, "tmdb": 1000, "imdb": "tt0000001", "title": "Test Show", "year": 2026}
        rows = {4000: [play_row(4000, 11, 95, 1)], 5000: [play_row(5000, 12, 97, 2)]}
        self.assertEqual(self.view(rows, crosswalk=crosswalk).distinct_viewers, 2)


if __name__ == "__main__":
    unittest.main()
