"""Source clients: result-level validation and stable pagination."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error

from curator.clients import SourceError, Tautulli

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

    def test_short_retrieval_is_reported_as_incomplete(self):
        """Fewer rows than declared must not read as a complete history."""
        client = Tautulli(
            "http://t",
            "k",
            opener=_Opener([history_page([play_row(1, 10, 95, 1)], 900)]),
        )
        _, meta = client.movie_history(page_size=500)
        self.assertFalse(meta["complete"])


if __name__ == "__main__":
    unittest.main()
