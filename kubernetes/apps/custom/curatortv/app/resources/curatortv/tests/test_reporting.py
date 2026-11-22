"""The weekly TV digest, which is the entire output of a read-only run.

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

from curatortv import reporting

NOW = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)


def show(series_id: int, title: str, size_gb: float, blocker: str = "") -> dict:
    """One entry as the plan serialises it."""
    return {
        "series_id": series_id,
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
            show(1, "Outcome", 12.0, "record has no usable added date"),
            show(2, "Deep Water", 8.0, "play history incomplete: coverage starts after release"),
        ],
        "missing_file": [],
        "candidates": [show(3, "The Devil's Mouth", 4.0)],
        "keep_tag_additions": [],
        "recycle_bin": "/media/trash, 14 days",
        "history": {"complete": True, "retrieved": 496},
        "unattributed_plays": 31,
    }
    base.update(overrides)
    return base


def judgement(**overrides) -> dict:
    """A judgement record, tied to the plan it was made about."""
    base = {"status": "judged", "detail": "", "plan_run_id": "run-test"}
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
        "deleted": [{"series_id": 3, "title": "The Devil's Mouth", "year": 2026, "size_gb": 4.0, "simulated": True}],
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
        self.assertIn("curatortv job", digest.push)

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
        acted = result(deleted=[{"series_id": 3, "title": "The Devil's Mouth", "year": 2026, "size_gb": 4.0}])
        digest = reporting.render(plan(mode="act"), acted, None, NOW)
        self.assertIn("DELETED 1", digest.push)

    def test_failed_and_uncertain_deletions_reach_the_push(self):
        bad = result(uncertain=[{"series_id": 9, "title": "Somewhere", "year": 2026, "why": "timed out"}])
        digest = reporting.render(plan(), bad, None, NOW)
        self.assertIn("1 deletion uncertain", digest.push)
        self.assertIn("timed out", digest.text)

    def test_a_skip_record_without_a_series_does_not_break_the_digest(self):
        """The activity-check-unavailable skip carries a reason and nothing else."""
        partial = result(skipped=[{"why": "activity check unavailable: timed out"}])
        digest = reporting.render(plan(), partial, None, NOW)
        self.assertIn("activity check unavailable", digest.text)


class ReviewQueueTests(unittest.TestCase):
    """The review queue is what the run is actually asking for."""

    def test_series_are_grouped_by_the_short_form_of_their_blocker(self):
        digest = reporting.render(plan(), result(), None, NOW)
        self.assertIn("1 × record has no usable added date", digest.push)
        self.assertNotIn("no request record", digest.push)

    def test_the_queue_size_and_volume_are_both_reported(self):
        digest = reporting.render(plan(), result(), None, NOW)
        self.assertIn("2 waiting on you", digest.push)
        self.assertIn("20 GB", digest.push)

    def test_a_series_with_no_blocker_falls_back_to_its_reason(self):
        spared = show(4, "Elsewhere", 1.0)
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
        spared = show(4, "Elsewhere", 1.0)
        spared["reasons"] = ["kept <b>because</b> & so on"]
        digest = reporting.render(plan(review=[spared]), None, None, NOW)
        self.assertIn("&lt;b&gt;", digest.push)


class JudgementRecordTests(unittest.TestCase):
    """Whether the model was asked, and what it said, is part of the record."""

    def test_a_failed_judgement_is_reported_not_hidden(self):
        digest = reporting.render(plan(), result(), judgement(status="failed", detail="npm install failed"), NOW)
        self.assertIn("npm install failed", digest.text)

    def test_a_failed_judgement_reaches_the_push(self):
        """Otherwise the phone shows a calm 'would delete 0' and nothing else."""
        digest = reporting.render(plan(), result(), judgement(status="failed", detail="npm install failed"), NOW)
        self.assertIn("judgement failed", digest.push)
        self.assertIn("judgement failed", digest.title)

    def test_last_weeks_judgement_is_not_reported_as_this_weeks(self):
        """It sits on the same volume, and says "judged" long after the fact."""
        stale = judgement(plan_run_id="run-last-week", verdicts={"keep": 3})
        digest = reporting.render(plan(), result(), stale, NOW)
        self.assertIn("no judgement was recorded for this plan", digest.text)
        self.assertNotIn("3 keep", digest.text)

    def test_nothing_to_judge_is_not_treated_as_a_problem(self):
        digest = reporting.render(plan(), result(), judgement(status="not asked", detail="no candidates"), NOW)
        self.assertNotIn("Needs attention", digest.push)


class EscalationTests(unittest.TestCase):
    """A series the judge sent to a person has to reach that person this week."""

    def escalated(self):
        """One result carrying a judge escalation."""
        return result(
            escalated=[{"series_id": 3, "title": "The Devil's Mouth", "year": 2026, "size_gb": 4.0, "reason": "unsure"}]
        )

    def test_an_escalated_series_joins_the_review_queue(self):
        digest = reporting.render(plan(), self.escalated(), None, NOW)
        self.assertIn("3 waiting on you", digest.push)

    def test_the_judges_reason_is_kept_in_the_report(self):
        digest = reporting.render(plan(), self.escalated(), None, NOW)
        self.assertIn("Sent to you by the judge", digest.text)
        self.assertIn("unsure", digest.text)

    def test_a_successful_judgement_reports_its_verdicts(self):
        record = judgement(verdicts={"keep": 3, "delete": 1})
        digest = reporting.render(plan(), result(), record, NOW)
        self.assertIn("1 delete, 3 keep", digest.text)

    def test_a_judgement_reports_the_tokens_it_used(self):
        record = judgement(verdicts={"delete": 4}, usage={"input_tokens": 61234, "output_tokens": 4321})
        digest = reporting.render(plan(), result(), record, NOW)
        self.assertIn("Judgement: 4 delete; 61,234 tokens in, 4,321 out.", digest.text)

    def test_token_counts_that_are_missing_or_not_numbers_are_left_out(self):
        for usage in (None, {}, {"input_tokens": 5}, {"input_tokens": "many", "output_tokens": 1}):
            with self.subTest(usage=usage):
                digest = reporting.render(plan(), result(), judgement(verdicts={"keep": 1}, usage=usage), NOW)
                self.assertIn("Judgement: 1 keep.", digest.text)

    def test_a_partial_judgement_shows_its_verdicts_and_is_a_problem(self):
        """Some batches answered and one did not: the verdicts are real, and the week is not clean."""
        record = judgement(
            status="partial",
            detail="2 of 3 batches judged; batch 002 (50 titles): rate limited",
            verdicts={"delete": 80, "keep": 20},
        )
        digest = reporting.render(plan(), result(unjudged=list(range(50))), record, NOW)
        self.assertIn("80 delete, 20 keep", digest.text)
        self.assertIn("INCOMPLETE: 2 of 3 batches judged", digest.text)
        self.assertIn("judgement partial", digest.title)
        self.assertIn("50 candidates came back with no verdict", digest.push)


class JudgeLimitTests(unittest.TestCase):
    """Candidates past the judge limit wait for a later run; that is not a failure."""

    def deferred(self) -> dict:
        """A plan offering one candidate and deferring two."""
        return plan(
            counts={"protected": 10, "candidate": 3},
            review=[],
            judge_limit=1,
            judge_deferred=[show(8, "Later", 3.0), show(9, "Later Still", 2.0)],
        )

    def test_the_digest_says_how_many_were_deferred(self):
        digest = reporting.render(self.deferred(), result(), judgement(), NOW)
        self.assertIn("Deferred: 2 candidates (5 GB) are past the judge limit of 1", digest.text)
        self.assertIn("3 candidates · 2 deferred", digest.push)

    def test_deferral_is_not_a_problem(self):
        digest = reporting.render(self.deferred(), result(), judgement(), NOW)
        self.assertEqual(digest.title, "TV cleanup: 0 to review")
        self.assertNotIn("Needs attention", digest.push)

    def test_nothing_is_said_when_nothing_was_deferred(self):
        digest = reporting.render(plan(), result(), judgement(), NOW)
        self.assertNotIn("Deferred", digest.text)
        self.assertNotIn("deferred", digest.push)


class WaitingOnYouTests(unittest.TestCase):
    """Only what the engine could not assess, and the judge's own reviews, reach a person."""

    def test_the_queue_is_the_plans_review_list_plus_the_judges_reviews(self):
        escalated = [{"series_id": 7, "title": "Unknown Quantity", "year": 2026, "size_gb": 3.0, "reason": "unsure"}]
        digest = reporting.render(plan(), result(escalated=escalated), judgement(), NOW)
        self.assertIn("## Waiting on you: 3 series", digest.text)

    def test_candidates_and_deferred_candidates_are_not_waiting_on_anyone(self):
        waiting = plan(review=[], judge_deferred=[show(8, "Later", 3.0)])
        digest = reporting.render(waiting, result(), judgement(), NOW)
        self.assertNotIn("Waiting on you", digest.text)
        self.assertNotIn("waiting on you", digest.push)


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


class TitleTests(unittest.TestCase):
    """The TV and film digests land on the same phone and must be told apart."""

    def test_every_title_names_tv(self):
        for digest in (
            reporting.render(None, None, None, NOW),
            reporting.render(plan(), result(), None, NOW),
            reporting.render(plan(blocks=["x"]), None, None, NOW),
        ):
            self.assertTrue(digest.title.startswith("TV cleanup"), digest.title)

    def test_judge_keeps_are_reported_as_kept_for_a_year(self):
        kept = result(
            judge_keeps={"due": 2, "kept": 2, "kept_until": "2027-11-28", "vetoed": [], "left_for_person": []}
        )
        text = reporting.render(plan(), kept, None, NOW).text
        self.assertIn("Kept for a year by the judge: 2 of 2 due", text)
        self.assertIn("Judged again after 2027-11-28", text)
        self.assertNotIn("Keep tag from the judge", text)


if __name__ == "__main__":
    unittest.main()
