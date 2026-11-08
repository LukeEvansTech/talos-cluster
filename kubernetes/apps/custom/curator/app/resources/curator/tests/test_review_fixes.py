"""Regression cover for the nine defects the Codex review found on the first cut.

Each test names the failure it prevents, because every one of these fails
*quietly*: the run reports success and the damage is invisible until someone
goes looking.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from curator.__main__ import apply_keep_tags, rehydrate
from curator.clients import SourceError
from curator.evidence import resolve_availability, resolve_viewing
from curator.executor import execute, reconcile
from curator.ledger import Ledger
from curator.model import Completion, HistoryStatus, Outcome, Viewing
from curator.planner import PlanContext, assess, fact_fingerprint, snapshot_valid

from .fixtures import (
    NOW,
    SCOPE_START,
    FakeRadarr,
    FakeS3,
    FakeTautulli,
    days_ago,
    feed_provenance,
    make_availability,
    make_film,
    make_viewing,
)
from .test_executor import TAGS, assessment, movie_record

# The moment the plan was made; the late-evidence gate compares against it.
PLANNED_AT = datetime.now(timezone.utc) - timedelta(minutes=30)


def context(**overrides) -> PlanContext:
    """A plan context with defaults for the fixed test clock."""
    base: dict[str, Any] = {
        "now": NOW,
        "scope_start": SCOPE_START,
        "monitored_collection_tmdb_ids": frozenset(),
        "coverage_start": days_ago(500),
        "history_ok": True,
        "decisions": {},
        "collections_loaded": True,
    }
    base.update(overrides)
    return PlanContext(**base)


class ReadFailureIsNotConfirmation(unittest.TestCase):
    """A failed lookup must never read as 'the film is gone'."""

    def test_transport_error_during_verification_is_uncertain(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        radarr.delete_behaviour[1] = SourceError("connection reset")
        # Revalidation reads fine; the verification read afterwards times out.
        radarr.read_errors_after[1] = (1, SourceError("gateway timeout"))
        s3 = FakeS3()
        result = execute(
            [assessment()],
            radarr,
            FakeTautulli(),
            Ledger(s3, "r1", dry_run=False),
            TAGS,
            set(),
            dry_run=False,
            planned_at=PLANNED_AT,
        )
        self.assertEqual(len(result.uncertain), 1)
        self.assertEqual(len(result.deleted), 0)

    def test_reconcile_does_not_claim_a_deletion_it_cannot_verify(self):
        radarr = FakeRadarr({}, TAGS)
        radarr.read_errors[7] = SourceError("500 from Radarr")
        ledger = Ledger(FakeS3(), "r2", dry_run=False)
        resolved = reconcile([{"run_id": "r1", "movie": {"movie_id": 7}}], radarr, ledger)
        self.assertEqual(resolved[0]["status"], "unresolved")


class RecoverabilityIsRecheckedLive(unittest.TestCase):
    """A stale plan must not authorise an unrecoverable delete."""

    def test_empty_recycle_bin_at_execution_blocks_everything(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        radarr.media_config = {"recycleBin": "", "recycleBinCleanupDays": 14}
        result = execute(
            [assessment()],
            radarr,
            FakeTautulli(),
            Ledger(FakeS3(), "r1", dry_run=False),
            TAGS,
            set(),
            dry_run=False,
            planned_at=PLANNED_AT,
        )
        self.assertEqual(radarr.deleted, [])
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("recoverability precondition", result.skipped[0]["why"])


class ActivityCheckCoversUnwatchedFilms(unittest.TestCase):
    """The films at risk of a first viewing are exactly the ones with no history."""

    def test_a_first_viewing_blocks_the_delete(self):
        radarr = FakeRadarr({1: movie_record()}, TAGS)
        tautulli = FakeTautulli(playing_ids={("tmdb", "1000")})
        result = execute(
            [assessment(rating_keys=())],
            radarr,
            tautulli,
            Ledger(FakeS3(), "r1", dry_run=False),
            TAGS,
            set(),
            dry_run=False,
            planned_at=PLANNED_AT,
        )
        self.assertEqual(radarr.deleted, [])
        self.assertIn("watching it right now", result.skipped[0]["why"])


class IntentCarriesRestoreState(unittest.TestCase):
    """Recovery needs fields that only exist before the delete."""

    def test_intent_records_path_profile_and_tags(self):
        live = movie_record()
        live.update({"path": "/films/Test Film (2026)", "qualityProfileId": 10, "tags": [4]})
        radarr = FakeRadarr({1: live}, TAGS)
        s3 = FakeS3()
        execute(
            [assessment()],
            radarr,
            FakeTautulli(),
            Ledger(s3, "r1", dry_run=True),
            TAGS,
            set(),
            dry_run=True,
            planned_at=PLANNED_AT,
        )
        intent = json.loads(s3.store["dry-run/runs/r1/intent/1.json"])
        restore = intent["movie"]["restore"]
        self.assertEqual(restore["path"], "/films/Test Film (2026)")
        self.assertEqual(restore["qualityProfileId"], 10)
        self.assertEqual(restore["tags"], [4])


class ReconciliationClosesTheOriginalIntent(unittest.TestCase):
    """An outcome filed under the wrong run leaves the intent dangling forever."""

    def test_outcome_is_written_under_the_intent_run(self):
        s3 = FakeS3()
        s3.put(
            "runs/r1/intent/9.json",
            json.dumps({"run_id": "r1", "movie": {"movie_id": 9}}).encode(),
        )
        ledger = Ledger(s3, "r2", dry_run=False)
        reconcile(ledger.unreconciled_intents(), FakeRadarr({}, TAGS), ledger)
        self.assertIn("runs/r1/outcome/9.json", s3.store)
        self.assertEqual(ledger.unreconciled_intents(), [])

    def test_a_reconciled_intent_is_not_re_counted_every_week(self):
        s3 = FakeS3()
        s3.put(
            "runs/r1/intent/9.json",
            json.dumps({"run_id": "r1", "movie": {"movie_id": 9}}).encode(),
        )
        ledger = Ledger(s3, "r2", dry_run=False)
        reconcile(ledger.unreconciled_intents(), FakeRadarr({}, TAGS), ledger)
        reconcile(ledger.unreconciled_intents(), FakeRadarr({}, TAGS), ledger)
        outcomes = [k for k in s3.list_keys("runs/") if "/outcome/" in k]
        self.assertEqual(len(outcomes), 1)


class DismissalWorksFromTheUiAlone(unittest.TestCase):
    """A person tagging a film in Radarr writes no ledger record."""

    def test_tag_alone_prevents_candidacy(self):
        film = make_film(tags={"cleanup-dismissed", "src-tmdb-popular"})
        result = assess(film, feed_provenance(), make_availability(), make_viewing(), context())
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("dismissed by a person", result.reasons[0])

    def test_a_changed_fact_still_reopens_it(self):
        film = make_film(tags={"cleanup-dismissed", "src-tmdb-popular"})
        decision = {"movie_id": film.movie_id, "fact_fingerprint": "something-older"}
        result = assess(
            film,
            feed_provenance(),
            make_availability(),
            make_viewing(),
            context(decisions={film.movie_id: decision}),
        )
        self.assertIs(result.outcome, Outcome.CANDIDATE)


class UpgradeCannotRestartTheWindowViaTheLedger(unittest.TestCase):
    """The earliest trustworthy observation wins, whichever source it came from."""

    def test_ledger_observation_older_than_the_retained_import_wins(self):
        film = make_film(added=days_ago(900))
        history = [{"eventType": "downloadFolderImported", "date": days_ago(2).isoformat()}]
        result = resolve_availability(film, history, days_ago(30), days_ago(300), NOW)
        self.assertEqual(result.first_playable, days_ago(300))
        self.assertEqual(result.source, "ledger")

    def test_a_genuinely_newer_ledger_entry_does_not_override_history(self):
        film = make_film()
        history = [{"eventType": "downloadFolderImported", "date": days_ago(200).isoformat()}]
        result = resolve_availability(film, history, days_ago(400), days_ago(100), NOW)
        self.assertEqual(result.first_playable, days_ago(200))


class SpareDecisionsStayBinding(unittest.TestCase):
    """A spare recorded against the wrong facts is stale the day it is made."""

    def test_rehydrate_preserves_completions(self):
        film = make_film()
        viewing = Viewing(
            status=HistoryStatus.OK,
            completions=(Completion(user_id=11, percent=97),),
            play_count=3,
            identity_via="tmdb",
        )
        planned = {
            "origin": "feed",
            "origin_evidence": [],
            "availability_source": "radarr-history",
            "history_status": "ok",
            "identity_via": "tmdb",
            "rating_keys": [],
            "completions": [{"user_id": 11, "percent": 97}],
            "play_count": 3,
        }
        restored = rehydrate(planned, film)
        self.assertEqual(restored.viewing.distinct_completers, 1)
        self.assertEqual(
            fact_fingerprint(film, feed_provenance(), restored.viewing, False),
            fact_fingerprint(film, feed_provenance(), viewing, False),
        )


class KeepTagsAreActuallyApplied(unittest.TestCase):
    """A computed allow-list that is never written protects nothing."""

    def test_act_mode_applies_and_verifies(self):
        record = movie_record()
        record["tags"] = []
        radarr = FakeRadarr({1: record}, {4: "keep"})

        def add_tag(movie_ids, tag_id):
            for movie_id in movie_ids:
                radarr.movies_by_id[movie_id]["tags"] = sorted(set(radarr.movies_by_id[movie_id]["tags"]) | {tag_id})
            return 202, None

        radarr.add_tag = add_tag
        result = apply_keep_tags(radarr, [{"movie_id": 1}], dry_run=False)
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["applied"], 1)

    def test_dry_run_applies_nothing(self):
        radarr = FakeRadarr({1: movie_record()}, {4: "keep"})
        result = apply_keep_tags(radarr, [{"movie_id": 1}], dry_run=True)
        self.assertEqual(result["applied"], 0)
        self.assertIn("dry run", result["note"])


if __name__ == "__main__":
    unittest.main()


class EmptyHistoryIsNeverAVerifiedZero(unittest.TestCase):
    """A wiped Tautulli answers healthily and declares nothing watched.

    Left unchecked the baseline is self-ratifying: recorded once, the next run
    sees no history-drop anomaly, has no coverage start to bound it, and reads
    every film in the library as a verified zero.
    """

    LIBRARY = {
        "library_size": 5000,
        "keep_tag_count": 4000,
        "collection_count": 700,
        "exclusion_count": 2500,
        "history_ok": True,
        "collections_loaded": True,
        "invalid_added_count": 0,
    }

    def test_zero_rows_is_refused_as_a_baseline(self):
        problems = snapshot_valid({**self.LIBRARY, "history_rows": 0})
        self.assertTrue(any("play history is empty" in p for p in problems))

    def test_a_stocked_library_with_history_is_still_accepted(self):
        self.assertEqual(snapshot_valid({**self.LIBRARY, "history_rows": 500}), [])

    def test_unbounded_coverage_is_not_trustworthy(self):
        result = resolve_viewing(make_film(), {}, {}, None, make_availability(), history_ok=True)
        self.assertIs(result.status, HistoryStatus.INCOMPLETE)
        self.assertFalse(result.trustworthy)

    def test_a_film_blocked_this_way_never_becomes_a_candidate(self):
        viewing = resolve_viewing(make_film(), {}, {}, None, make_availability(), history_ok=True)
        result = assess(make_film(), feed_provenance(), make_availability(), viewing, context())
        self.assertIs(result.outcome, Outcome.REVIEW)
