"""Cases where a check that did not run must not read as a check that passed.

Each of these is a safeguard that already existed and could still be bypassed,
because somewhere between the service and the decision an outage was quietly
converted into a negative answer.
"""

from __future__ import annotations

import json
import unittest
import urllib.error

from curator.__main__ import apply_keep_tags, source_blocks
from curator.clients import ResultError, SourceError, Tautulli
from curator.executor import validate_recommendations
from curator.model import Film
from curator.planner import keep_tag_applies

from .fixtures import FakeRadarr, make_film


class _Opener:
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

    def pending(self) -> int:
        """How many queued responses are left."""
        return len(self.bodies)


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
        """Swallowed, it let now_playing_ids drop the film being watched."""
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
                    envelope("success", {"sessions": [{"rating_key": 77}]}),
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
        self.assertIn("requested film could not be told from a feed pull", blocks[0])

    def test_healthy_sources_block_nothing(self):
        self.assertEqual(source_blocks(True, "bin ok", True, "", requests_ok=True), [])

    def test_each_outage_is_named_separately(self):
        blocks = source_blocks(False, "recycle bin empty", False, "short read", requests_ok=False)
        self.assertEqual(len(blocks), 3)


class KeepTagHandoffTests(unittest.TestCase):
    """The plan is half an hour old by the time the tag is applied."""

    def film(self, tags=()) -> Film:
        """A film that qualifies for the allow list."""
        return make_film(movie_id=1, tags=frozenset(tags), imdb_score=8.0, imdb_votes=90000)

    def test_a_film_marked_eligible_since_planning_is_not_re_protected(self):
        self.assertFalse(keep_tag_applies(self.film({"cleanup-eligible"})))

    def test_an_untouched_film_still_qualifies(self):
        self.assertTrue(keep_tag_applies(self.film()))

    def test_apply_withdraws_a_target_whose_tags_changed(self):
        live = {1: {"id": 1, "title": "A", "tags": [9], "ratings": {"imdb": {"value": 8.0, "votes": 90000}}}}
        outcome = apply_keep_tags(
            FakeRadarr({}),
            [{"movie_id": 1}],
            dry_run=True,
            live=live,
            tag_labels={9: "cleanup-eligible"},
        )
        self.assertEqual(outcome["withdrawn"], 1)
        self.assertEqual(outcome["requested"], 0)

    def test_a_target_that_vanished_from_radarr_is_withdrawn(self):
        outcome = apply_keep_tags(FakeRadarr({}), [{"movie_id": 1}], dry_run=True, live={}, tag_labels={})
        self.assertEqual(outcome["withdrawn"], 1)


class DuplicateVerdictTests(unittest.TestCase):
    """One film, judged twice, has been judged two ways."""

    def test_the_second_verdict_for_a_film_is_rejected(self):
        accepted, rejected = validate_recommendations(
            [
                {"movie_id": 1, "verdict": "delete", "reason": "feed filler, nobody played it"},
                {"movie_id": 1, "verdict": "keep", "reason": "completes a collection"},
            ],
            {1: object()},
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["verdict"], "delete")
        self.assertEqual(rejected[0]["why"], "judged more than once")


if __name__ == "__main__":
    unittest.main()
