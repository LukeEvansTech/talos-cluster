"""Source clients: result-level validation and stable pagination."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error

from curator.clients import RequestSystem, SourceError, Tautulli

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

    def open(self, request, timeout=None):  # pylint: disable=unused-argument
        """Return the next scripted response; timeout matches urllib's signature."""
        self.urls.append(request.full_url if hasattr(request, "full_url") else str(request))
        payload = self.responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _Response(payload)


def history_page(rows, declared):
    """A Tautulli get_history envelope."""
    return {
        "response": {
            "result": "success",
            "data": {
                "data": rows,
                "recordsFiltered": declared,
                "recordsTotal": declared,
            },
        }
    }


class TautulliValidationTests(unittest.TestCase):
    """A 200 is not a success; the envelope has its own result field."""

    def test_result_error_raises(self):
        opener = _Opener([{"response": {"result": "error", "message": "bad key", "data": None}}])
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=opener).preflight()

    def test_empty_preflight_payload_raises(self):
        opener = _Opener([{"response": {"result": "success", "data": ""}}])
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=opener).preflight()

    def test_unexpected_history_structure_raises(self):
        opener = _Opener([{"response": {"result": "success", "data": {"nope": 1}}}])
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=opener).movie_history()

    def test_transport_failure_raises_rather_than_returning_empty(self):
        opener = _Opener([urllib.error.URLError("connection refused")])
        with self.assertRaises(SourceError):
            Tautulli("http://t", "k", opener=opener).preflight()


class TautulliPaginationTests(unittest.TestCase):
    """More rows than one page, walked without losing or double-counting any."""

    def test_walks_every_page_beyond_two_thousand_rows(self):
        page_size = 500
        total = 2600
        pages = []
        for start in range(0, total, page_size):
            rows = [play_row(5000, 10 + (i % 7), 95, row_id=i) for i in range(start, min(start + page_size, total))]
            pages.append(history_page(rows, total))
        pages.append(history_page([], total))
        client = Tautulli("http://t", "k", opener=_Opener(pages))
        rows, meta = client.movie_history(page_size=page_size)
        self.assertEqual(len(rows), total)
        self.assertEqual(meta["declared"], total)
        self.assertTrue(meta["complete"])

    def test_ascending_order_is_requested(self):
        """New plays must land at the end, not shift every row mid-walk."""
        opener = _Opener([history_page([], 0)])
        Tautulli("http://t", "k", opener=opener).movie_history()
        self.assertIn("order_dir=asc", opener.urls[0])
        self.assertIn("grouping=0", opener.urls[0])

    def test_rows_repeated_across_pages_are_counted_once(self):
        page_size = 2
        first = history_page([play_row(1, 10, 95, 1), play_row(1, 11, 95, 2)], 4)
        shifted = history_page([play_row(1, 11, 95, 2), play_row(1, 12, 95, 3)], 4)
        last = history_page([play_row(1, 13, 95, 4)], 4)
        client = Tautulli("http://t", "k", opener=_Opener([first, shifted, last]))
        rows, meta = client.movie_history(page_size=page_size)
        self.assertEqual(len({r["row_id"] for r in rows}), len(rows))
        self.assertEqual(meta["duplicates_skipped"], 1)

    def test_an_in_progress_session_does_not_make_history_read_as_incomplete(self):
        """Tautulli lists a playing session with no row id, and it counts.

        Dropped, `retrieved` came up one short of the declared count, the run
        blocked on "play history not usable", and nothing happened -- every time
        somebody happened to be watching a film when the job fired.
        """
        playing = play_row(1, 10, 34, 1)
        playing["row_id"] = playing["id"] = None
        client = Tautulli("http://t", "k", opener=_Opener([history_page([play_row(1, 11, 95, 2), playing], 2)]))
        rows, meta = client.movie_history(page_size=500)
        self.assertEqual(len(rows), 2)
        self.assertEqual(meta["without_id"], 1)
        self.assertTrue(meta["complete"])

    def test_short_retrieval_is_reported_as_incomplete(self):
        """Fewer rows than declared must not read as a complete history."""
        client = Tautulli(
            "http://t",
            "k",
            opener=_Opener([history_page([play_row(1, 10, 95, 1)], 900)]),
        )
        _, meta = client.movie_history(page_size=500)
        self.assertFalse(meta["complete"])


class RequestSystemTests(unittest.TestCase):
    """A film requested twice keeps both ends: who first asked, and who asked last."""

    def request(self, tmdb_id: int, who: str, created: str | None, kind: str = "movie") -> dict:
        """One request as the request system lists it."""
        return {"type": kind, "media": {"tmdbId": tmdb_id}, "requestedBy": {"displayName": who}, "createdAt": created}

    def requests(self, *items: dict) -> dict[int, dict]:
        """What the client makes of one page of requests."""
        payload = {"pageInfo": {"results": len(items)}, "results": list(items)}
        return RequestSystem("http://r", "k", opener=_Opener([payload])).movie_requests()

    def test_a_single_request_is_both_the_first_and_the_latest(self):
        record = self.requests(self.request(603, "Sam", "2026-03-01T10:00:00.000Z"))[603]
        self.assertEqual((record["requested_by"], record["latest_by"]), ("Sam", "Sam"))
        self.assertEqual(record["created_at"], record["latest_at"])
        self.assertFalse(record["undated"])

    def test_two_requests_keep_the_earliest_as_origin_and_the_newest_beside_it(self):
        old = self.request(603, "Sam", "2026-03-01T10:00:00.000Z")
        new = self.request(603, "Alex", "2026-09-11T10:00:00.000Z")
        for order in ((old, new), (new, old)):
            with self.subTest(first=order[0]["requestedBy"]["displayName"]):
                record = self.requests(*order)[603]
                self.assertEqual((record["requested_by"], record["created_at"]), ("Sam", "2026-03-01T10:00:00.000Z"))
                self.assertEqual((record["latest_by"], record["latest_at"]), ("Alex", "2026-09-11T10:00:00.000Z"))

    def test_a_request_with_no_date_marks_the_film_undated(self):
        dated = self.request(603, "Sam", "2026-03-01T10:00:00.000Z")
        for order in ((dated, self.request(603, "Alex", None)), (self.request(603, "Alex", None), dated)):
            with self.subTest(first=order[0]["requestedBy"]["displayName"]):
                self.assertTrue(self.requests(*order)[603]["undated"])

    def test_series_requests_are_not_film_requests(self):
        self.assertEqual(self.requests(self.request(603, "Sam", "2026-03-01T10:00:00.000Z", kind="tv")), {})


if __name__ == "__main__":
    unittest.main()
