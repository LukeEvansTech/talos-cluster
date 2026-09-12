"""The weekly digest, which is the entire output of a read-only run.

Every case here is one where the digest could say something reassuring and
wrong: no plan at all, a plan left over from last week, a blocked run, or a
dry run whose "0 deleted" means nothing was allowed rather than nothing was due.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from curator import reporting

NOW = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)


def film(movie_id: int, title: str, size_gb: float, blocker: str = "") -> dict:
    """One entry as the plan serialises it."""
    return {
        "movie_id": movie_id,
        "title": title,
        "year": 2026,
        "size_gb": size_gb,
        "blockers": [blocker] if blocker else [],
        "reasons": [],
    }


def plan(**overrides) -> dict:
    """A plan document with the fields the digest reads."""
    base = {
        "run_id": "run-test",
        "mode": "dry-run",
        "generated_at": (NOW - timedelta(minutes=30)).isoformat(),
        "counts": {"protected": 4405, "out_of_scope": 1210, "review": 2, "candidate": 1},
        "blocks": [],
        "review": [
            film(1, "Outcome", 12.0, "origin unknown: no request record and no provenance tag"),
            film(2, "Deep Water", 8.0, "play history incomplete: coverage starts after release"),
        ],
        "missing_file": [],
        "candidates": [film(3, "The Devil's Mouth", 4.0)],
        "keep_tag_additions": [],
        "recycle_bin": "/media/trash, 14 days",
        "history": {"complete": True, "retrieved": 496},
        "unattributed_plays": 31,
    }
    base.update(overrides)
    return base


def result(**overrides) -> dict:
    """An execution summary in its dry-run shape."""
    base = {
        # Deliberately not the plan's: the executor is a separate CronJob and
        # generates its own. `plan_run_id` is what ties the two together.
        "run_id": "run-execute",
        "plan_run_id": "run-test",
        "mode": "dry-run",
        "escalated": [],
        "deleted": [{"movie_id": 3, "title": "The Devil's Mouth", "year": 2026, "size_gb": 4.0, "simulated": True}],
        "skipped": [],
        "failed": [],
        "uncertain": [],
        "deferred": [],
        "rejected_recommendations": [],
        "keep_tags_added": {"applied": 0},
    }
    base.update(overrides)
    return base


class AbsentEvidenceTests(unittest.TestCase):
    """A digest that cannot see a run must not describe one."""

    def test_no_plan_is_reported_as_no_plan(self):
        digest = reporting.render(None, None, None, NOW)
        self.assertIn("no plan", digest.title)
        self.assertIn("curator job", digest.push)

    def test_last_weeks_plan_is_not_reported_as_this_weeks(self):
        """The worst failure here: a stale plan reads as a healthy quiet week."""
        stale = plan(generated_at=(NOW - timedelta(days=7)).isoformat())
        digest = reporting.render(stale, result(), None, NOW)
        self.assertIn("stale", digest.title)
        self.assertNotIn("would delete", digest.push)

    def test_a_plan_with_no_timestamp_is_not_trusted(self):
        digest = reporting.render(plan(generated_at=None), None, None, NOW)
        self.assertIn("unreadable", digest.title)

    def test_a_missing_result_is_named_rather_than_read_as_nothing_to_do(self):
        digest = reporting.render(plan(), None, None, NOW)
        self.assertIn("did not finish", digest.text)

    def test_last_weeks_result_is_not_reported_against_this_weeks_plan(self):
        """Both files live on the same volume; only the plan ID tells them apart."""
        digest = reporting.render(plan(), result(plan_run_id="run-last-week"), None, NOW)
        self.assertIn("did not finish", digest.text)
        self.assertNotIn("would delete", digest.push)

    def test_this_weeks_result_is_used_even_though_its_own_run_id_differs(self):
        """The normal case: two CronJobs, two run IDs, one plan between them."""
        digest = reporting.render(plan(), result(), None, NOW)
        self.assertIn("would delete 1", digest.push)
        self.assertNotIn("did not finish", digest.text)

    def test_an_unfinished_execution_reaches_the_push_not_only_the_text(self):
        digest = reporting.render(plan(), None, None, NOW)
        self.assertIn("execute did not finish", digest.push)
        self.assertIn("execute did not finish", digest.title)


class BlockedRunTests(unittest.TestCase):
    """A blocked run is the one a person most needs to see."""

    def test_blocks_lead_the_title_and_the_push(self):
        digest = reporting.render(plan(blocks=["recycle bin is not configured"]), None, None, NOW)
        self.assertIn("BLOCKED", digest.title)
        self.assertIn("recycle bin is not configured", digest.push)
        self.assertIn("## Blocked", digest.text)


class DryRunTests(unittest.TestCase):
    """A simulated deletion must never read as a real one."""

    def test_a_simulated_deletion_says_would(self):
        digest = reporting.render(plan(), result(), None, NOW)
        self.assertIn("would delete 1", digest.push)
        self.assertIn("Would delete: 1", digest.text)

    def test_a_real_deletion_says_deleted(self):
        acted = result(deleted=[{"movie_id": 3, "title": "The Devil's Mouth", "year": 2026, "size_gb": 4.0}])
        digest = reporting.render(plan(mode="act"), acted, None, NOW)
        self.assertIn("DELETED 1", digest.push)

    def test_failed_and_uncertain_deletions_reach_the_push(self):
        bad = result(uncertain=[{"movie_id": 9, "title": "Somewhere", "year": 2026, "why": "timed out"}])
        digest = reporting.render(plan(), bad, None, NOW)
        self.assertIn("1 deletion uncertain", digest.push)
        self.assertIn("timed out", digest.text)

    def test_a_skip_record_without_a_film_does_not_break_the_digest(self):
        """The activity-check-unavailable skip carries a reason and nothing else."""
        partial = result(skipped=[{"why": "activity check unavailable: timed out"}])
        digest = reporting.render(plan(), partial, None, NOW)
        self.assertIn("activity check unavailable", digest.text)


class ReviewQueueTests(unittest.TestCase):
    """The review queue is what the run is actually asking for."""

    def test_films_are_grouped_by_the_short_form_of_their_blocker(self):
        digest = reporting.render(plan(), result(), None, NOW)
        self.assertIn("1 × origin unknown", digest.push)
        self.assertNotIn("no request record", digest.push)

    def test_the_queue_size_and_volume_are_both_reported(self):
        digest = reporting.render(plan(), result(), None, NOW)
        self.assertIn("2 waiting on you", digest.push)
        self.assertIn("20 GB", digest.push)

    def test_a_film_with_no_blocker_falls_back_to_its_reason(self):
        spared = film(4, "Elsewhere", 1.0)
        spared["reasons"] = ["already spared on 2026-08-01: part of a collection"]
        digest = reporting.render(plan(review=[spared]), result(), None, NOW)
        self.assertIn("already spared on 2026-08-01", digest.push)


class RenderingLimitTests(unittest.TestCase):
    """Pushover silently truncates; this cuts where it can be marked."""

    def test_a_long_push_is_clipped_and_says_so(self):
        """Anomaly blocks are free text and are the one part with no length cap."""
        wordy = [f"{i}: " + "the baseline and this run disagree about the library " * 6 for i in range(4)]
        digest = reporting.render(plan(blocks=wordy), result(), None, NOW)
        self.assertLessEqual(len(digest.push), reporting.PUSH_LIMIT + 20)
        self.assertIn("truncated", digest.push)

    def test_model_authored_text_reaching_the_push_is_escaped(self):
        """A spared reason is the judge's own words, and it lands in a push."""
        spared = film(4, "Elsewhere", 1.0)
        spared["reasons"] = ["kept <b>because</b> & so on"]
        digest = reporting.render(plan(review=[spared]), None, None, NOW)
        self.assertIn("&lt;b&gt;", digest.push)


class JudgementRecordTests(unittest.TestCase):
    """Whether the model was asked, and what it said, is part of the record."""

    def test_a_failed_judgement_is_reported_not_hidden(self):
        digest = reporting.render(plan(), result(), {"status": "failed", "detail": "npm install failed"}, NOW)
        self.assertIn("npm install failed", digest.text)

    def test_a_failed_judgement_reaches_the_push(self):
        """Otherwise the phone shows a calm 'would delete 0' and nothing else."""
        digest = reporting.render(plan(), result(), {"status": "failed", "detail": "npm install failed"}, NOW)
        self.assertIn("judgement failed", digest.push)
        self.assertIn("judgement failed", digest.title)

    def test_nothing_to_judge_is_not_treated_as_a_problem(self):
        digest = reporting.render(plan(), result(), {"status": "not asked", "detail": "no candidates"}, NOW)
        self.assertNotIn("Needs attention", digest.push)


class EscalationTests(unittest.TestCase):
    """A film the judge sent to a person has to reach that person this week."""

    def escalated(self):
        """One result carrying a judge escalation."""
        return result(
            escalated=[{"movie_id": 3, "title": "The Devil's Mouth", "year": 2026, "size_gb": 4.0, "reason": "unsure"}]
        )

    def test_an_escalated_film_joins_the_review_queue(self):
        digest = reporting.render(plan(), self.escalated(), None, NOW)
        self.assertIn("3 waiting on you", digest.push)

    def test_the_judges_reason_is_kept_in_the_report(self):
        digest = reporting.render(plan(), self.escalated(), None, NOW)
        self.assertIn("Sent to you by the judge", digest.text)
        self.assertIn("unsure", digest.text)

    def test_a_successful_judgement_reports_its_verdicts(self):
        record = {"status": "judged", "verdicts": {"keep": 3, "delete": 1}, "cost_usd": 0.42}
        digest = reporting.render(plan(), result(), record, NOW)
        self.assertIn("1 delete, 3 keep", digest.text)


class FakeOpener:
    """Stands in for urllib's opener, recording what it was asked to send."""

    def __init__(self, status: int = 200, error: Exception | None = None):
        self.status = status
        self.error = error
        self.request: Any = None
        self.timeout: int | None = None

    def open(self, request, timeout=None):
        """Record the request and answer with the configured status."""
        self.request, self.timeout = request, timeout
        if self.error:
            raise self.error
        return nullcontext(SimpleNamespace(status=self.status))

    def header(self, name: str) -> str | None:
        """The header the last request carried."""
        return self.request.get_header(name)


class DeliveryTests(unittest.TestCase):
    """An undelivered digest looks exactly like a week the job did not run."""

    def setUp(self):
        self.digest = reporting.Digest(title="t", push="p", text="x")

    def test_the_route_token_is_sent_as_a_header(self):
        opener = FakeOpener()
        reporting.post("http://chaski/hooks/library-cleanup", "secret", self.digest, opener=opener)
        self.assertEqual(opener.header("X-chaski-token"), "secret")
        self.assertEqual(json.loads(opener.request.data)["title"], "t")

    def test_a_rejected_post_raises(self):
        opener = FakeOpener(error=urllib.error.HTTPError("u", 401, "no", {}, None))
        with self.assertRaises(reporting.NotificationError):
            reporting.post("http://chaski/hooks/library-cleanup", "wrong", self.digest, opener=opener)

    def test_an_unreachable_webhook_raises(self):
        opener = FakeOpener(error=urllib.error.URLError("refused"))
        with self.assertRaises(reporting.NotificationError):
            reporting.post("http://chaski/hooks/library-cleanup", "secret", self.digest, opener=opener)


if __name__ == "__main__":
    unittest.main()
