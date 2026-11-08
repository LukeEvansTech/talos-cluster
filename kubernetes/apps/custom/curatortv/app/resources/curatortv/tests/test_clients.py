"""Source clients: result-level validation, stable pagination and show identity."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error

from curatortv.clients import RequestSystem, Sonarr, SourceError, Tautulli, find_request

from .fixtures import play_row


class _Response(io.BytesIO):  # pylint: disable=too-few-public-methods
    """Just enough of an HTTP response for urllib's context-manager use."""

    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        """Enter the context manager."""
        return self

    def __exit__(self, *exc):
        """Close the buffer on exit."""
        self.close()
        return False


class _Opener:  # pylint: disable=too-few-public-methods
    """Replays scripted responses and records the URLs asked for."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls: list[str] = []
        self.methods: list[str] = []

    def open(self, request, timeout=None):  # pylint: disable=unused-argument
        """Return the next scripted response; timeout matches urllib's signature."""
        self.urls.append(request.full_url if hasattr(request, "full_url") else str(request))
        self.methods.append(request.get_method() if hasattr(request, "get_method") else "GET")
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _Response(payload)


def envelope(data, result="success") -> dict:
    """A Tautulli API envelope."""
    return {"response": {"result": result, "data": data}}


def history_page(rows, declared):
    """A Tautulli get_history envelope."""
    return envelope({"data": rows, "recordsFiltered": declared, "recordsTotal": declared})


class TautulliValidationTests(unittest.TestCase):
    """A 200 is not a success; the envelope has its own result field."""

    def test_result_error_raises(self):
        opener = _Opener([{"response": {"result": "error", "message": "bad key", "data": None}}])
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=opener).preflight()

    def test_empty_preflight_payload_raises(self):
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=_Opener([envelope("")])).preflight()

    def test_unexpected_history_structure_raises(self):
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=_Opener([envelope({"nope": 1})])).episode_history()

    def test_transport_failure_raises_rather_than_returning_empty(self):
        opener = _Opener([urllib.error.URLError("connection refused")])
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=opener).preflight()


class TautulliPaginationTests(unittest.TestCase):
    """More rows than one page, walked without losing or double-counting any."""

    def test_walks_every_page_beyond_two_thousand_rows(self):
        page_size, total = 500, 2600
        pages = []
        for start in range(0, total, page_size):
            rows = [play_row(5000, 10 + (i % 7), 95, row_id=i) for i in range(start, min(start + page_size, total))]
            pages.append(history_page(rows, total))
        pages.append(history_page([], total))
        rows, meta = Tautulli("http://t", "k", opener=_Opener(pages)).episode_history(page_size=page_size)
        self.assertEqual(len(rows), total)
        self.assertTrue(meta["complete"])

    def test_episode_rows_ascending_and_ungrouped_are_requested(self):
        opener = _Opener([history_page([], 0)])
        Tautulli("http://t", "k", opener=opener).episode_history()
        self.assertIn("media_type=episode", opener.urls[0])
        self.assertIn("order_dir=asc", opener.urls[0])
        self.assertIn("grouping=0", opener.urls[0])

    def test_rows_repeated_across_pages_are_counted_once(self):
        first = history_page([play_row(1, 10, 95, 1), play_row(1, 11, 95, 2)], 4)
        shifted = history_page([play_row(1, 11, 95, 2), play_row(1, 12, 95, 3)], 4)
        last = history_page([play_row(1, 13, 95, 4)], 4)
        rows, meta = Tautulli("http://t", "k", opener=_Opener([first, shifted, last])).episode_history(page_size=2)
        self.assertEqual(len({r["row_id"] for r in rows}), len(rows))
        self.assertEqual(meta["duplicates_skipped"], 1)

    def test_an_in_progress_session_does_not_make_history_read_as_incomplete(self):
        """Tautulli lists a playing session with no row id, and counts it."""
        playing = play_row(1, 10, 34, 1)
        playing["row_id"] = playing["id"] = None
        opener = _Opener([history_page([play_row(1, 11, 95, 2), playing], 2)])
        rows, meta = Tautulli("http://t", "k", opener=opener).episode_history(page_size=500)
        self.assertEqual(len(rows), 2)
        self.assertEqual(meta["without_id"], 1)
        self.assertTrue(meta["complete"])

    def test_short_retrieval_is_reported_as_incomplete(self):
        opener = _Opener([history_page([play_row(1, 10, 95, 1)], 900)])
        _, meta = Tautulli("http://t", "k", opener=opener).episode_history(page_size=500)
        self.assertFalse(meta["complete"])


class NowPlayingTests(unittest.TestCase):
    """A session is matched through its show, never its episode."""

    def test_episode_sessions_resolve_to_show_ids(self):
        opener = _Opener(
            [
                envelope({"sessions": [{"media_type": "episode", "rating_key": 9, "grandparent_rating_key": 77}]}),
                envelope({"guids": ["imdb://tt0452046", "tmdb://4057", "tvdb://75710"], "media_type": "show"}),
            ]
        )
        ids = Tautulli("http://t", "k", opener=opener).now_playing_ids()
        self.assertEqual(ids, {("imdb", "tt0452046"), ("tmdb", "4057"), ("tvdb", "75710")})
        self.assertIn("rating_key=77", opener.urls[1])

    def test_movie_sessions_are_ignored(self):
        opener = _Opener([envelope({"sessions": [{"media_type": "movie", "rating_key": 9}]})])
        self.assertEqual(Tautulli("http://t", "k", opener=opener).now_playing_ids(), set())

    def test_an_episode_session_with_no_show_key_raises(self):
        """A short answer here reads as "nobody is watching it"."""
        opener = _Opener([envelope({"sessions": [{"media_type": "episode", "rating_key": 9}]})])
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=opener).now_playing_ids()


class SonarrTests(unittest.TestCase):
    """The few Sonarr calls whose shape matters for safety."""

    def test_import_events_are_grouped_by_series(self):
        page = {
            "totalRecords": 3,
            "records": [
                {"eventType": "downloadFolderImported", "seriesId": 1, "episodeId": 10, "date": "2026-03-01T00:00:00Z"},
                {"eventType": "downloadFolderImported", "seriesId": 1, "episodeId": 11, "date": "2026-03-02T00:00:00Z"},
                {"eventType": "grabbed", "seriesId": 2, "episodeId": 20, "date": "2026-03-03T00:00:00Z"},
            ],
        }
        opener = _Opener([page])
        events = Sonarr("http://s", "k", opener=opener).import_events()
        self.assertEqual(sorted(events), [1])
        self.assertEqual(len(events[1]), 2)
        self.assertIn("eventType=3", opener.urls[0])

    def test_delete_adds_an_import_list_exclusion(self):
        opener = _Opener([None])
        Sonarr("http://s", "k", opener=opener).delete_series(5)
        self.assertEqual(opener.methods[0], "DELETE")
        self.assertIn("/api/v3/series/5?deleteFiles=true&addImportListExclusion=true", opener.urls[0])

    def test_a_404_is_gone_but_a_500_is_raised(self):
        gone = urllib.error.HTTPError("u", 404, "missing", {}, None)
        broken = urllib.error.HTTPError("u", 500, "broken", {}, None)
        self.addCleanup(gone.close)
        self.addCleanup(broken.close)
        client = Sonarr("http://s", "k", opener=_Opener([gone, broken]))
        self.assertIsNone(client.series(1))
        with self.assertRaises(SourceError):
            client.series(1)


class RequestSystemTests(unittest.TestCase):
    """TV requests are keyed by TVDB and TMDb both."""

    def test_tv_requests_index_both_ids_and_skip_films(self):
        payload = {
            "pageInfo": {"results": 2},
            "results": [
                {"type": "tv", "media": {"tvdbId": 75710, "tmdbId": 4057}, "requestedBy": {"displayName": "Luke"}},
                {"type": "movie", "media": {"tmdbId": 603}},
            ],
        }
        requests = RequestSystem("http://r", "k", opener=_Opener([payload])).tv_requests()
        self.assertEqual(set(requests), {("tvdb", 75710), ("tmdb", 4057)})
        self.assertEqual(find_request(requests, 75710, None)["requested_by"], "Luke")
        self.assertIsNotNone(find_request(requests, 1, 4057))
        self.assertIsNone(find_request(requests, 1, 2))


if __name__ == "__main__":
    unittest.main()
