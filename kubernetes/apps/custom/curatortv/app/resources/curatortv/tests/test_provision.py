"""Decision-tag provisioning: what it would change, and that it changes nothing by default."""

from __future__ import annotations

import unittest

from curatortv.provision import plan_changes


class _FakeSonarr:  # pylint: disable=too-few-public-methods
    """Enough of the Sonarr surface for provisioning."""

    def __init__(self, labels):
        self._tags = [{"id": i, "label": label} for i, label in enumerate(labels, start=1)]

    def tags(self):
        """Tag definitions."""
        return self._tags


class PlanTests(unittest.TestCase):
    """What provisioning would do, without doing it."""

    def test_missing_decision_tags_are_reported(self):
        wanted, absent = plan_changes(_FakeSonarr([]))
        self.assertEqual(wanted, ["cleanup-keep", "cleanup-dismissed", "cleanup-eligible"])
        self.assertEqual(absent, ["kometa"])

    def test_a_provisioned_instance_needs_nothing(self):
        wanted, absent = plan_changes(_FakeSonarr(["kometa", "cleanup-keep", "cleanup-dismissed", "cleanup-eligible"]))
        self.assertEqual(wanted, [])
        self.assertEqual(absent, [])


if __name__ == "__main__":
    unittest.main()
