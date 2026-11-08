"""Regression cover ported from the film engine's review fixes.

Each test names the failure it prevents, because every one of these fails
*quietly*: the run reports success and the damage is invisible until someone
goes looking.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from curatortv.__main__ import rehydrate
from curatortv.clients import SourceError
from curatortv.evidence import resolve_viewing
from curatortv.executor import execute, reconcile
from curatortv.ledger import Ledger
from curatortv.model import EpisodeProgress, HistoryStatus, Outcome, Viewing
from curatortv.planner import PlanContext, assess, fact_fingerprint, snapshot_valid

from .fixtures import (
    NOW,
    SCOPE_START,
    FakeS3,
    FakeSonarr,
    FakeTautulli,
    days_ago,
    feed_provenance,
    make_availability,
    make_show,
    make_viewing,
    series_record,
)
from .test_executor import TAGS, assessment

PLANNED_AT = datetime.now(timezone.utc) - timedelta(minutes=30)


def context(**overrides) -> PlanContext:
    """A plan context with defaults for the fixed test clock."""
    base: dict[str, Any] = {
        "now": NOW,
        "scope_start": SCOPE_START,
        "coverage_start": days_ago(500),
        "history_ok": True,
        "decisions": {},
    }
    base.update(overrides)
    return PlanContext(**base)


def run_execute(sonarr, ledger, tautulli=None, approved=None, dry_run=False):
    """Execute one batch with test defaults."""
    return execute(
        approved or [assessment()],
        sonarr,
        tautulli or FakeTautulli(),
        ledger,
        TAGS,
        dry_run=dry_run,
        planned_at=PLANNED_AT,
    )


class ReadFailureIsNotConfirmation(unittest.TestCase):
    """A failed lookup must never read as 'the series is gone'."""

    def test_transport_error_during_verification_is_uncertain(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        sonarr.delete_behaviour[1] = SourceError("connection reset")
        sonarr.read_errors_after[1] = (1, SourceError("gateway timeout"))
        result = run_execute(sonarr, Ledger(FakeS3(), "r1", dry_run=False))
        self.assertEqual(len(result.uncertain), 1)
        self.assertEqual(len(result.deleted), 0)

    def test_reconcile_does_not_claim_a_deletion_it_cannot_verify(self):
        sonarr = FakeSonarr({}, TAGS)
        sonarr.read_errors[7] = SourceError("500 from Sonarr")
        resolved = reconcile(
            [{"run_id": "r1", "series": {"series_id": 7}}], sonarr, Ledger(FakeS3(), "r2", dry_run=False)
        )
        self.assertEqual(resolved[0]["status"], "unresolved")


class RecoverabilityIsRecheckedLive(unittest.TestCase):
    """A stale plan must not authorise an unrecoverable delete."""

    def test_empty_recycle_bin_at_execution_blocks_everything(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        sonarr.media_config = {"recycleBin": "", "recycleBinCleanupDays": 14}
        result = run_execute(sonarr, Ledger(FakeS3(), "r1", dry_run=False))
        self.assertEqual(sonarr.delete_calls, [])
        self.assertIn("recoverability precondition", result.skipped[0]["why"])


class ActivityCheckCoversUnwatchedSeries(unittest.TestCase):
    """The series at risk of a first viewing are exactly the ones with no history."""

    def test_a_first_viewing_blocks_the_delete(self):
        sonarr = FakeSonarr({1: series_record()}, TAGS)
        tautulli = FakeTautulli(playing_ids={("tvdb", "70001")})
        result = run_execute(sonarr, Ledger(FakeS3(), "r1", dry_run=False), tautulli, [assessment(rating_keys=())])
        self.assertEqual(sonarr.delete_calls, [])
        self.assertIn("watching it right now", result.skipped[0]["why"])


class IntentCarriesRestoreState(unittest.TestCase):
    """Recovery needs fields that only exist before the delete."""

    def test_intent_records_path_profile_and_tags(self):
        live = series_record()
        live.update({"path": "/tv/Test Show", "qualityProfileId": 10, "tags": [4]})
        s3 = FakeS3()
        run_execute(FakeSonarr({1: live}, TAGS), Ledger(s3, "r1", dry_run=True), dry_run=True)
        restore = json.loads(s3.store["dry-run/runs/r1/intent/1.json"])["series"]["restore"]
        self.assertEqual(restore["path"], "/tv/Test Show")
        self.assertEqual(restore["qualityProfileId"], 10)
        self.assertEqual(restore["tags"], [4])
        self.assertEqual(restore["tvdbId"], 70001)


class ReconciliationClosesTheOriginalIntent(unittest.TestCase):
    """An outcome filed under the wrong run leaves the intent dangling forever."""

    def setUp(self):
        self.s3 = FakeS3()
        self.s3.put("runs/r1/intent/9.json", json.dumps({"run_id": "r1", "series": {"series_id": 9}}).encode())
        self.ledger = Ledger(self.s3, "r2", dry_run=False)

    def test_outcome_is_written_under_the_intent_run(self):
        reconcile(self.ledger.unreconciled_intents(), FakeSonarr({}, TAGS), self.ledger)
        self.assertIn("runs/r1/outcome/9.json", self.s3.store)
        self.assertEqual(self.ledger.unreconciled_intents(), [])

    def test_a_reconciled_intent_is_not_re_counted_every_week(self):
        reconcile(self.ledger.unreconciled_intents(), FakeSonarr({}, TAGS), self.ledger)
        reconcile(self.ledger.unreconciled_intents(), FakeSonarr({}, TAGS), self.ledger)
        self.assertEqual(len([k for k in self.s3.list_keys("runs/") if "/outcome/" in k]), 1)


class DismissalWorksFromTheUiAlone(unittest.TestCase):
    """A person tagging a series in Sonarr writes no ledger record."""

    def test_tag_alone_prevents_candidacy(self):
        show = make_show(tags={"cleanup-dismissed", "kometa"})
        result = assess(show, feed_provenance(), make_availability(), make_viewing(), context())
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("dismissed by a person", result.reasons[0])

    def test_a_changed_fact_still_reopens_it(self):
        show = make_show(tags={"cleanup-dismissed", "kometa"})
        decision = {"series_id": show.series_id, "fact_fingerprint": "something-older"}
        result = assess(show, feed_provenance(), make_availability(), make_viewing(), context(decisions={1: decision}))
        self.assertIs(result.outcome, Outcome.CANDIDATE)


class SpareDecisionsStayBinding(unittest.TestCase):
    """A spare recorded against the wrong facts is stale the day it is made."""

    def test_rehydrate_preserves_viewers(self):
        show = make_show()
        viewing = Viewing(
            status=HistoryStatus.OK,
            progress=(EpisodeProgress(user_id=11, episodes=4),),
            play_count=6,
            identity_via="tvdb",
        )
        planned = {
            "origin": "feed",
            "origin_evidence": [],
            "availability_source": "sonarr-history",
            "history_status": "ok",
            "identity_via": "tvdb",
            "rating_keys": [],
            "viewers": [{"user_id": 11, "episodes": 4}],
            "play_count": 6,
        }
        restored = rehydrate(planned, show)
        self.assertEqual(restored.viewing.distinct_viewers, 1)
        self.assertEqual(restored.viewing.episodes_completed, 4)
        self.assertEqual(
            fact_fingerprint(show, feed_provenance(), restored.viewing),
            fact_fingerprint(show, feed_provenance(), viewing),
        )


class EmptyHistoryIsNeverAVerifiedZero(unittest.TestCase):
    """A wiped Tautulli answers healthily and declares nothing watched."""

    LIBRARY = {
        "library_size": 900,
        "keep_tag_count": 400,
        "exclusion_count": 4,
        "history_ok": True,
        "invalid_added_count": 0,
    }

    def test_zero_rows_is_refused_as_a_baseline(self):
        problems = snapshot_valid({**self.LIBRARY, "history_rows": 0})
        self.assertTrue(any("play history is empty" in p for p in problems))

    def test_a_stocked_library_with_history_is_still_accepted(self):
        self.assertEqual(snapshot_valid({**self.LIBRARY, "history_rows": 500}), [])

    def test_a_series_blocked_this_way_never_becomes_a_candidate(self):
        viewing = resolve_viewing(make_show(), {}, {}, None, make_availability(), history_ok=True)
        result = assess(make_show(), feed_provenance(), make_availability(), viewing, context())
        self.assertIs(result.outcome, Outcome.REVIEW)


if __name__ == "__main__":
    unittest.main()
