"""Durable state: namespacing, write-once rules, overlap and reconciliation."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from curatortv.ledger import LOCK_KEY, Ledger

from .fixtures import FakeS3


def now() -> datetime:
    """Current UTC time."""
    return datetime.now(timezone.utc)


class NamespacingTests(unittest.TestCase):
    """A rehearsal must never be mistaken for the real record."""

    def test_dry_run_writes_under_its_own_prefix(self):
        s3 = FakeS3()
        Ledger(s3, "r1", dry_run=True).write_baseline({"library_size": 10})
        self.assertIn("dry-run/baseline/current.json", s3.store)
        self.assertNotIn("baseline/current.json", s3.store)

    def test_act_writes_at_the_root(self):
        s3 = FakeS3()
        Ledger(s3, "r1", dry_run=False).write_baseline({"library_size": 10})
        self.assertIn("baseline/current.json", s3.store)

    def test_a_dry_run_cannot_read_real_state_into_its_own(self):
        s3 = FakeS3()
        Ledger(s3, "r1", dry_run=False).write_baseline({"library_size": 10})
        self.assertIsNone(Ledger(s3, "r2", dry_run=True).read_baseline())


class AdoptRehearsalTests(unittest.TestCase):
    """The first act run begins from what the dry runs left, once, and never over a real record."""

    def rehearsed(self) -> FakeS3:
        """A store holding what dry runs leave: a baseline, a proposal, a decision and a run."""
        s3 = FakeS3()
        dry = Ledger(s3, "r1", dry_run=True)
        dry.write_baseline({"library_size": 10})
        dry.write_proposal({"series_id": 7, "verdict": "delete"})
        dry.write_decision({"series_id": 8, "kind": "spare"})
        dry.record_plan({"run_id": "r1"})
        return s3

    def test_the_standing_record_comes_over(self):
        s3 = self.rehearsed()
        act = Ledger(s3, "r2", dry_run=False)
        self.assertEqual(act.adopt_rehearsal(), 3)
        self.assertEqual(act.read_baseline()["library_size"], 10)
        self.assertEqual(act.read_proposals()[7]["verdict"], "delete")
        self.assertEqual(act.read_decisions()[8]["kind"], "spare")

    def test_a_rehearsed_run_is_no_part_of_the_real_history(self):
        s3 = self.rehearsed()
        Ledger(s3, "r2", dry_run=False).adopt_rehearsal()
        self.assertEqual([key for key in s3.store if key.startswith("runs/")], [])

    def test_the_rehearsal_keeps_its_own_copy(self):
        s3 = self.rehearsed()
        Ledger(s3, "r2", dry_run=False).adopt_rehearsal()
        self.assertEqual(list(Ledger(s3, "r3", dry_run=True).read_proposals()), [7])

    def test_a_real_record_already_begun_is_left_alone(self):
        s3 = self.rehearsed()
        act = Ledger(s3, "r2", dry_run=False)
        act.write_baseline({"library_size": 99})
        self.assertEqual(act.adopt_rehearsal(), 0)
        self.assertEqual(act.read_proposals(), {})
        self.assertEqual(act.read_baseline()["library_size"], 99)

    def test_it_happens_once(self):
        s3 = self.rehearsed()
        act = Ledger(s3, "r2", dry_run=False)
        act.adopt_rehearsal()
        act.delete_proposal(7)
        self.assertEqual(act.adopt_rehearsal(), 0)
        self.assertEqual(act.read_proposals(), {})

    def test_a_dry_run_adopts_nothing(self):
        s3 = self.rehearsed()
        before = dict(s3.store)
        self.assertEqual(Ledger(s3, "r2", dry_run=True).adopt_rehearsal(), 0)
        self.assertEqual(s3.store, before)

    def test_with_no_rehearsal_there_is_nothing_to_adopt(self):
        act = Ledger(FakeS3(), "r1", dry_run=False)
        self.assertEqual(act.adopt_rehearsal(), 0)
        self.assertIsNone(act.read_baseline())


class FirstSeenTests(unittest.TestCase):
    """The availability fallback may only ever move backwards in time."""

    def test_first_write_is_stored(self):
        s3 = FakeS3()
        ledger = Ledger(s3, "r1", dry_run=False)
        stamp = now() - timedelta(days=10)
        ledger.write_first_seen({5: stamp})
        self.assertEqual(ledger.read_first_seen()[5], stamp)

    def test_a_later_observation_does_not_overwrite_an_earlier_one(self):
        """An upgrade or rename must not restart the grace period."""
        s3 = FakeS3()
        ledger = Ledger(s3, "r1", dry_run=False)
        original = now() - timedelta(days=100)
        ledger.write_first_seen({5: original})
        ledger.write_first_seen({5: now()})
        self.assertEqual(ledger.read_first_seen()[5], original)

    def test_corrupt_entries_are_ignored_not_fatal(self):
        s3 = FakeS3()
        s3.put("firstseen/index.json", json.dumps({"5": "not-a-date"}).encode())
        self.assertEqual(Ledger(s3, "r1", dry_run=False).read_first_seen(), {})


class CrosswalkTests(unittest.TestCase):
    """Retired Plex keys still carry old plays."""

    def test_merge_keeps_historical_keys(self):
        s3 = FakeS3()
        ledger = Ledger(s3, "r1", dry_run=False)
        ledger.write_crosswalk({4000: {"tmdb": 1, "imdb": "tt1"}})
        ledger.write_crosswalk({5000: {"tmdb": 1, "imdb": "tt1"}})
        stored = ledger.read_crosswalk()
        self.assertIn(4000, stored)
        self.assertIn(5000, stored)


class DecisionTests(unittest.TestCase):
    """Standing decisions survive the run that made them."""

    def test_round_trip(self):
        s3 = FakeS3()
        ledger = Ledger(s3, "r1", dry_run=False)
        ledger.write_decision({"series_id": 7, "verdict": "spared", "reason": "well-known series"})
        self.assertEqual(ledger.read_decisions()[7]["verdict"], "spared")

    def test_corrupt_decision_is_skipped(self):
        s3 = FakeS3()
        s3.put("decisions/7.json", b"{not json")
        self.assertEqual(Ledger(s3, "r1", dry_run=False).read_decisions(), {})


class OverlapTests(unittest.TestCase):
    """Two runs must not spend the same budget twice."""

    def test_deletions_in_window_counts_only_confirmed_recent_ones(self):
        s3 = FakeS3()
        ledger = Ledger(s3, "r2", dry_run=False)
        s3.put(
            "runs/r1/outcome/1.json",
            json.dumps({"status": "deleted", "recorded_at": now().isoformat()}).encode(),
        )
        s3.put(
            "runs/r1/outcome/2.json",
            json.dumps({"status": "uncertain", "recorded_at": now().isoformat()}).encode(),
        )
        s3.put(
            "runs/r0/outcome/3.json",
            json.dumps(
                {
                    "status": "deleted",
                    "recorded_at": (now() - timedelta(days=30)).isoformat(),
                }
            ).encode(),
        )
        self.assertEqual(ledger.deletions_in_window(days=7), 1)

    def test_lock_blocks_a_second_run(self):
        s3 = FakeS3()
        first = Ledger(s3, "r1", dry_run=False)
        self.assertTrue(first.acquire_lock()[0])
        ok, why = Ledger(s3, "r2", dry_run=False).acquire_lock()
        self.assertFalse(ok)
        self.assertIn("r1", why)

    def test_an_expired_lock_is_ignored(self):
        s3 = FakeS3()
        s3.put(
            LOCK_KEY,
            json.dumps(
                {
                    "run_id": "r1",
                    "expires_at": (now() - timedelta(minutes=5)).isoformat(),
                }
            ).encode(),
        )
        self.assertTrue(Ledger(s3, "r2", dry_run=False).acquire_lock()[0])

    def test_release_only_drops_your_own_lock(self):
        s3 = FakeS3()
        Ledger(s3, "r1", dry_run=False).acquire_lock()
        Ledger(s3, "r2", dry_run=False).release_lock()
        self.assertIn(LOCK_KEY, s3.store)


class ReconciliationTests(unittest.TestCase):
    """Intents with no outcome are the interrupted deletions."""

    def test_intent_without_outcome_is_returned(self):
        s3 = FakeS3()
        ledger = Ledger(s3, "r2", dry_run=False)
        s3.put(
            "runs/r1/intent/9.json",
            json.dumps({"run_id": "r1", "series": {"series_id": 9}}).encode(),
        )
        self.assertEqual(len(ledger.unreconciled_intents()), 1)

    def test_intent_with_an_outcome_is_settled(self):
        s3 = FakeS3()
        ledger = Ledger(s3, "r2", dry_run=False)
        s3.put(
            "runs/r1/intent/9.json",
            json.dumps({"run_id": "r1", "series": {"series_id": 9}}).encode(),
        )
        s3.put("runs/r1/outcome/9.json", json.dumps({"status": "deleted"}).encode())
        self.assertEqual(ledger.unreconciled_intents(), [])

    def test_simulated_intents_are_never_reconciled(self):
        """A dry run's intent describes something that never happened."""
        s3 = FakeS3()
        ledger = Ledger(s3, "r2", dry_run=False)
        s3.put(
            "runs/r1/intent/9.json",
            json.dumps({"run_id": "r1", "series": {"series_id": 9}, "simulated": True}).encode(),
        )
        self.assertEqual(ledger.unreconciled_intents(), [])

    def test_dry_run_records_no_outcome_at_all(self):
        s3 = FakeS3()
        Ledger(s3, "r1", dry_run=True).record_outcome(1, "deleted", "should not be written")
        self.assertEqual(s3.store, {})


if __name__ == "__main__":
    unittest.main()
