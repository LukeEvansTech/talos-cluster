"""Provenance provisioning: what it would change, and that it changes nothing by default."""

from __future__ import annotations

import unittest

from curator.provision import plan_changes, slug


class _FakeRadarr:  # pylint: disable=too-few-public-methods
    """Enough of the Radarr surface for provisioning."""

    def __init__(self, tags, lists):
        self._tags = tags
        self._lists = lists

    def tags(self):
        """Tag definitions."""
        return self._tags

    def import_lists(self):
        """Import list definitions."""
        return self._lists


class SlugTests(unittest.TestCase):
    """Tag suffixes must be stable and readable."""

    def test_punctuation_and_case_are_normalised(self):
        self.assertEqual(slug("TMDb Popular (6.5+ rating)"), "tmdb-popular-6-5-rating")

    def test_empty_name_still_produces_a_tag(self):
        self.assertEqual(slug("!!!"), "list")


class PlanTests(unittest.TestCase):
    """What provisioning would do, without doing it."""

    def test_missing_tags_and_lists_are_reported(self):
        radarr = _FakeRadarr([], [{"id": 1, "name": "StevenLu", "tags": []}])
        changes, wanted = plan_changes(radarr)
        self.assertIn("src-stevenlu", wanted)
        self.assertIn("cleanup-eligible", wanted)
        self.assertEqual(changes[0]["tag"], "src-stevenlu")

    def test_already_tagged_list_needs_no_change(self):
        radarr = _FakeRadarr(
            [{"id": 9, "label": "src-stevenlu"}]
            + [
                {"id": i, "label": t}
                for i, t in enumerate(("cleanup-keep", "cleanup-dismissed", "cleanup-eligible"), start=20)
            ],
            [{"id": 1, "name": "StevenLu", "tags": [9]}],
        )
        changes, wanted = plan_changes(radarr)
        self.assertEqual(changes, [])
        self.assertEqual(wanted, [])


if __name__ == "__main__":
    unittest.main()
