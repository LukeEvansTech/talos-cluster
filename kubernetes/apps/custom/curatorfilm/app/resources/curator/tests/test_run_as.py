"""A run started early: which scheduled run it may act as, and when it is refused."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from curator import __main__ as cli
from curator import proposals

from .test_review_window import LONDON, SATURDAYS, at


class StandInTests(unittest.TestCase):
    """A run started early may act as the next scheduled run, and as no other."""

    TUESDAY = at(2026, 11, 24, 14, 0)
    NEXT = at(2026, 11, 28, 9, 30)

    def test_unset_there_is_none(self):
        self.assertEqual(proposals.stand_in("", self.TUESDAY, SATURDAYS, LONDON), (None, ""))

    def test_the_next_firing_named_to_the_minute_is_taken(self):
        self.assertEqual(proposals.stand_in("2026-11-28T09:30:00Z", self.TUESDAY, SATURDAYS, LONDON), (self.NEXT, ""))
        self.assertEqual(
            proposals.stand_in("2026-11-28T10:30:00+01:00", self.TUESDAY, SATURDAYS, LONDON), (self.NEXT, "")
        )

    def test_it_takes_what_the_page_listed_and_nothing_due_later(self):
        ahead, _ = proposals.stand_in("2026-11-28T09:30:00Z", self.TUESDAY, SATURDAYS, LONDON)
        listed = {"due_at": at(2026, 11, 27, 21, 49).isoformat()}
        after = {"due_at": at(2026, 11, 28, 9, 31).isoformat()}
        self.assertFalse(proposals.is_due(listed, proposals.run_start(self.TUESDAY, SATURDAYS, LONDON)))
        self.assertTrue(proposals.is_due(listed, ahead))
        self.assertFalse(proposals.is_due(after, ahead))

    def test_any_other_moment_is_refused(self):
        for named in ("2026-11-28T09:31:00Z", "2026-12-05T09:30:00Z", "2026-11-21T09:30:00Z", "2030-01-01T00:00:00Z"):
            with self.subTest(named=named):
                ahead, why = proposals.stand_in(named, self.TUESDAY, SATURDAYS, LONDON)
                self.assertIsNone(ahead)
                self.assertIn("the next scheduled run is 2026-11-28T09:30:00+00:00", why)

    def test_left_on_a_job_it_is_refused_once_that_run_has_passed(self):
        ahead, why = proposals.stand_in("2026-11-28T09:30:00Z", at(2026, 11, 28, 9, 30), SATURDAYS, LONDON)
        self.assertIsNone(ahead)
        self.assertIn("2026-12-05T09:30:00+00:00", why)

    def test_what_cannot_be_read_is_refused(self):
        self.assertIn("not a date and time", proposals.stand_in("saturday", self.TUESDAY, SATURDAYS, LONDON)[1])
        self.assertIn("CLEANUP_SCHEDULE", proposals.stand_in("2026-11-28T09:30:00Z", self.TUESDAY)[1])
        self.assertIn("CLEANUP_SCHEDULE", proposals.stand_in("2026-11-28T09:30:00Z", self.TUESDAY, "30 9 * * SAT")[1])


class DueAsOfTests(unittest.TestCase):
    """What execute measures "due" against, from its own settings."""

    TUESDAY = at(2026, 11, 24, 14, 0)

    def read(self, **settings: str):
        """What a Tuesday run with these settings measures against."""
        with mock.patch.dict(os.environ, {"CLEANUP_SCHEDULE": SATURDAYS, "TZ": LONDON, **settings}):
            return cli.due_as_of(self.TUESDAY)

    def test_a_run_started_by_hand_is_the_last_firing(self):
        self.assertEqual(self.read(), (at(2026, 11, 21, 9, 30), ""))

    def test_told_to_act_as_the_next_run_it_is_that_run(self):
        self.assertEqual(self.read(CLEANUP_RUN_AS="2026-11-28T09:30:00Z"), (at(2026, 11, 28, 9, 30), ""))

    def test_too_far_ahead_for_the_cap_to_count_it_names_no_moment(self):
        """The cap looks back six days from the scheduled run's deletes; a day of that is kept in hand."""
        settings = {"CLEANUP_SCHEDULE": SATURDAYS, "TZ": LONDON, "CLEANUP_RUN_AS": "2026-11-28T09:30:00Z"}
        with mock.patch.dict(os.environ, settings):
            for early in (at(2026, 11, 21, 10, 30), at(2026, 11, 23, 9, 29)):
                started, why = cli.due_as_of(early)
                self.assertIsNone(started)
                self.assertIn("the cap might not count this run", why)
            self.assertEqual(cli.due_as_of(at(2026, 11, 23, 9, 30))[0], at(2026, 11, 28, 9, 30))

    def test_told_to_act_as_any_other_it_names_no_moment(self):
        started, why = self.read(CLEANUP_RUN_AS="2026-12-05T09:30:00Z")
        self.assertIsNone(started)
        self.assertIn("CLEANUP_RUN_AS", why)


if __name__ == "__main__":
    unittest.main()
