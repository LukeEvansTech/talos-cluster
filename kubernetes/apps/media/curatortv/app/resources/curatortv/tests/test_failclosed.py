"""Cases where a check that did not run must not read as a check that passed.

Each of these is a safeguard that already existed and could still be bypassed,
because somewhere between the service and the decision an outage was quietly
converted into a negative answer.
"""

from __future__ import annotations

import json
import unittest
import urllib.error

from curatortv.__main__ import apply_keep_tags, group_by_show, source_blocks
from curatortv.clients import ResultError, SourceError, Tautulli

from .fixtures import FakeSonarr, play_row


class _Opener:  # pylint: disable=too-few-public-methods
    """Answers with a queued body, or raises."""

    def __init__(self, bodies=None, error: Exception | None = None):
        self.bodies = list(bodies or [])
        self.error = error

    def open(self, request, timeout=None):  # pylint: disable=unused-argument
        """Return the next queued response, or raise a queued exception."""
        if self.error:
            raise self.error
        nxt = self.bodies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _Body(nxt)


class _Body:
    """Minimal response object."""

    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        """The encoded body."""
        return json.dumps(self.payload).encode()


def envelope(result: str, data) -> dict:
    """A Tautulli API envelope."""
    return {"response": {"result": result, "data": data}}


class MetadataFailuresTests(unittest.TestCase):
    """A timeout is not the same answer as "no such item"."""

    def test_a_refusal_is_reported_as_absent(self):
        client = Tautulli("http://t", "k", opener=_Opener([envelope("error", None)]))
        self.assertIsNone(client.metadata(1))

    def test_a_transport_failure_is_raised_not_swallowed(self):
        client = Tautulli("http://t", "k", opener=_Opener(error=urllib.error.URLError("timeout")))
        with self.assertRaises(SourceError):
            client.metadata(1)

    def test_a_refusal_raises_the_narrower_error(self):
        client = Tautulli("http://t", "k", opener=_Opener([envelope("error", None)]))
        with self.assertRaises(ResultError):
            client.preflight()

    def test_now_playing_ids_fails_rather_than_returning_a_short_set(self):
        """A short set of active sessions reads as "nobody is watching it"."""
        client = Tautulli(
            "http://t",
            "k",
            opener=_Opener(
                [
                    envelope("success", {"sessions": [{"media_type": "episode", "grandparent_rating_key": 77}]}),
                    urllib.error.URLError("timeout"),
                ]
            ),
        )
        with self.assertRaises(SourceError):
            client.now_playing_ids()


class SourceOutageTests(unittest.TestCase):
    """A source that did not answer stops the run rather than narrowing it."""

    def test_an_unreachable_request_system_blocks(self):
        blocks = source_blocks(True, "bin ok", True, "", requests_ok=False)
        self.assertEqual(len(blocks), 1)
        self.assertIn("requested show could not be told from a feed pull", blocks[0])

    def test_healthy_sources_block_nothing(self):
        self.assertEqual(source_blocks(True, "bin ok", True, "", requests_ok=True), [])

    def test_each_outage_is_named_separately(self):
        self.assertEqual(len(source_blocks(False, "recycle bin empty", False, "short read", requests_ok=False)), 3)


class UnkeyedPlaysTests(unittest.TestCase):
    """A play that names no show is still a play."""

    def test_rows_without_a_show_key_are_kept_aside_not_dropped(self):
        orphan = play_row(1, 10, 95, 2)
        orphan["grandparent_rating_key"] = None
        by_key, unkeyed = group_by_show([play_row(1, 10, 95, 1), orphan])
        self.assertEqual(list(by_key), [1])
        self.assertEqual(unkeyed, [orphan])


class KeepTagHandoffTests(unittest.TestCase):
    """The plan is half an hour old by the time the tag is applied."""

    def test_apply_withdraws_a_target_whose_tags_changed(self):
        live = {1: {"id": 1, "title": "A", "tags": [9], "ratings": {"value": 8.0, "votes": 90000}}}
        outcome = apply_keep_tags(
            FakeSonarr({}), [{"series_id": 1}], dry_run=True, live=live, tag_labels={9: "cleanup-eligible"}
        )
        self.assertEqual(outcome["withdrawn"], 1)
        self.assertEqual(outcome["requested"], 0)

    def test_a_target_that_vanished_from_sonarr_is_withdrawn(self):
        outcome = apply_keep_tags(FakeSonarr({}), [{"series_id": 1}], dry_run=True, live={}, tag_labels={})
        self.assertEqual(outcome["withdrawn"], 1)


if __name__ == "__main__":
    unittest.main()
