"""The filesystem ledger backend."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from curator.ledger import Ledger
from curator.store import FileStore


class FileStoreTests(unittest.TestCase):
    """Same contract as the S3 backend, over a directory."""

    def setUp(self):
        # A `with` cannot span setUp and the test body; addCleanup is
        # unittest's own idiom for exactly this case.
        # pylint: disable=consider-using-with
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = FileStore(self._tmp.name)

    def test_round_trip_through_nested_keys(self):
        self.store.put("runs/r1/intent/7.json", b'{"a":1}')
        self.assertEqual(json.loads(self.store.get("runs/r1/intent/7.json")), {"a": 1})

    def test_missing_key_returns_none(self):
        self.assertIsNone(self.store.get("nope.json"))

    def test_list_keys_filters_by_prefix(self):
        self.store.put("runs/r1/intent/7.json", b"{}")
        self.store.put("decisions/7.json", b"{}")
        self.assertEqual(self.store.list_keys("runs/"), ["runs/r1/intent/7.json"])

    def test_delete_is_idempotent(self):
        self.store.delete("never-existed.json")
        self.store.put("x.json", b"{}")
        self.store.delete("x.json")
        self.assertEqual(self.store.list_keys(""), [])

    def test_a_key_cannot_escape_the_root(self):
        """Keys are internal, but one that escaped would write anywhere."""
        with self.assertRaises(ValueError):
            self.store.put("../escape.json", b"x")

    def test_writes_are_atomic(self):
        """A run killed mid-write must not leave a half-written intent record.

        The next run parses those files to decide whether a deletion is still
        outstanding, so a truncated one is worse than a missing one.
        """
        self.store.put("runs/r1/intent/7.json", b'{"complete":true}')
        leftovers = [p.name for p in Path(self._tmp.name).rglob(".*.tmp")]
        self.assertEqual(leftovers, [])

    def test_partial_files_are_not_listed(self):
        Path(self._tmp.name, ".half.json.tmp").write_bytes(b"{")
        self.assertEqual(self.store.list_keys(""), [])


class LedgerOverFileStoreTests(unittest.TestCase):
    """The ledger does not care which backend it has."""

    def setUp(self):
        # A `with` cannot span setUp and the test body; addCleanup is
        # unittest's own idiom for exactly this case.
        # pylint: disable=consider-using-with
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = FileStore(self._tmp.name)

    def test_dry_run_still_namespaces_its_writes(self):
        Ledger(self.store, "r1", dry_run=True).write_baseline({"library_size": 10})
        self.assertEqual(self.store.list_keys(""), ["dry-run/baseline/current.json"])

    def test_decisions_round_trip(self):
        ledger = Ledger(self.store, "r1", dry_run=False)
        ledger.write_decision({"movie_id": 7, "verdict": "spared", "reason": "collection"})
        self.assertEqual(ledger.read_decisions()[7]["verdict"], "spared")

    def test_dry_run_records_no_outcome(self):
        Ledger(self.store, "r1", dry_run=True).record_outcome(1, "deleted", "nope")
        self.assertEqual(self.store.list_keys(""), [])


if __name__ == "__main__":
    unittest.main()
