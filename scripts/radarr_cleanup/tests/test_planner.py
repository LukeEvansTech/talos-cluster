"""Scope, protection, batching and anomaly rules."""

from __future__ import annotations

import unittest
from datetime import timedelta

from radarr_cleanup.model import HistoryStatus, Origin, Outcome, Provenance
from radarr_cleanup.planner import (
    MAX_DELETIONS_PER_RUN,
    PlanContext,
    assess,
    build_decision_record,
    check_anomalies,
    fact_fingerprint,
    select_batch,
    snapshot_valid,
)

from .fixtures import (
    NOW,
    SCOPE_START,
    days_ago,
    feed_provenance,
    make_availability,
    make_film,
    make_viewing,
    unknown_provenance,
)


def context(**overrides) -> PlanContext:
    """A plan context with sane defaults for the fixed test clock."""
    base = {
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


def run(film=None, provenance=None, availability=None, viewing=None, ctx=None):
    """Assess one film with fixture defaults."""
    return assess(
        film or make_film(),
        provenance or feed_provenance(),
        availability or make_availability(),
        viewing or make_viewing(),
        ctx or context(),
    )


class ProtectionTests(unittest.TestCase):
    """Vetoes are checked before anything else and are absolute."""

    def test_keep_tag_protects(self):
        result = run(make_film(tags={"keep"}))
        self.assertIs(result.outcome, Outcome.PROTECTED)

    def test_keep_review_tag_protects(self):
        self.assertIs(run(make_film(tags={"keep-review"})).outcome, Outcome.PROTECTED)

    def test_human_keep_tag_protects(self):
        self.assertIs(run(make_film(tags={"cleanup-keep"})).outcome, Outcome.PROTECTED)

    def test_monitored_collection_protects_even_without_a_file(self):
        film = make_film(tmdb_id=555, has_file=False, tags=frozenset())
        result = run(film, ctx=context(monitored_collection_tmdb_ids=frozenset({555})))
        self.assertIs(result.outcome, Outcome.PROTECTED)
        self.assertIn("monitored collection", result.protections[0])

    def test_missing_collection_data_blocks_rather_than_permits(self):
        result = run(ctx=context(collections_loaded=False))
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("collection data unavailable", result.blockers[0])

    def test_requested_film_is_protected(self):
        provenance = Provenance(origin=Origin.REQUESTED, requested_by="Luke")
        result = run(provenance=provenance)
        self.assertIs(result.outcome, Outcome.PROTECTED)


class ScopeTests(unittest.TestCase):
    """Scope, windows and files."""

    def test_unreleased_is_out_of_scope(self):
        self.assertIs(run(make_film(status="announced")).outcome, Outcome.OUT_OF_SCOPE)

    def test_pre_scope_is_out_of_scope(self):
        self.assertIs(run(make_film(added=days_ago(400))).outcome, Outcome.OUT_OF_SCOPE)

    def test_unusable_added_date_goes_to_review(self):
        result = run(make_film(added=None))
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("added date", result.blockers[0])

    def test_missing_file_has_its_own_outcome(self):
        result = run(make_film(has_file=False), availability=make_availability(days=120))
        self.assertIs(result.outcome, Outcome.MISSING_FILE)

    def test_inside_the_window_is_not_due(self):
        result = run(availability=make_availability(days=10))
        self.assertIs(result.outcome, Outcome.NOT_DUE)
        self.assertEqual(result.window_days, 90)

    def test_low_score_uses_the_short_window(self):
        film = make_film(imdb_score=4.2)
        self.assertIs(run(film, availability=make_availability(days=40)).outcome, Outcome.CANDIDATE)
        self.assertIs(run(film, availability=make_availability(days=20)).outcome, Outcome.NOT_DUE)


class EvidenceGateTests(unittest.TestCase):
    """A zero play count only counts when it is trustworthy."""

    def test_unavailable_history_blocks(self):
        result = run(viewing=make_viewing(status=HistoryStatus.UNAVAILABLE))
        self.assertIs(result.outcome, Outcome.REVIEW)

    def test_incomplete_history_blocks(self):
        self.assertIs(run(viewing=make_viewing(status=HistoryStatus.INCOMPLETE)).outcome, Outcome.REVIEW)

    def test_unresolved_identity_blocks(self):
        self.assertIs(run(viewing=make_viewing(status=HistoryStatus.UNRESOLVED)).outcome, Outcome.REVIEW)

    def test_two_completions_go_to_the_human_queue(self):
        result = run(viewing=make_viewing(completers=2))
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("2 distinct users", result.reasons[0])

    def test_one_completion_does_not_protect(self):
        self.assertIs(run(viewing=make_viewing(completers=1)).outcome, Outcome.CANDIDATE)


class AuthorisationTests(unittest.TestCase):
    """Delete-by-default needs an authorisation; date of arrival is not one."""

    def test_feed_provenance_authorises(self):
        self.assertIs(run(provenance=feed_provenance()).outcome, Outcome.CANDIDATE)

    def test_unknown_origin_goes_to_review_not_delete(self):
        result = run(make_film(tags=frozenset()), provenance=unknown_provenance())
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("origin unknown", result.blockers[0])

    def test_human_eligible_tag_authorises_without_relabelling_origin(self):
        film = make_film(tags={"cleanup-eligible"})
        result = run(film, provenance=unknown_provenance())
        self.assertIs(result.outcome, Outcome.CANDIDATE)
        self.assertIs(result.provenance.origin, Origin.UNKNOWN)
        self.assertIn("cleanup-eligible", result.reasons[0])

    def test_human_eligible_still_loses_to_a_veto(self):
        film = make_film(tags={"cleanup-eligible", "keep"})
        self.assertIs(run(film, provenance=unknown_provenance()).outcome, Outcome.PROTECTED)

    def test_human_eligible_still_loses_to_a_monitored_collection(self):
        film = make_film(tmdb_id=77, tags={"cleanup-eligible"})
        result = run(film, ctx=context(monitored_collection_tmdb_ids=frozenset({77})))
        self.assertIs(result.outcome, Outcome.PROTECTED)


class StandingDecisionTests(unittest.TestCase):
    """A settled judgement should not be re-litigated every week."""

    def test_a_binding_spare_suppresses_the_candidate(self):
        film = make_film()
        viewing = make_viewing()
        fingerprint = fact_fingerprint(film, feed_provenance(), viewing, False)
        decision = {
            "movie_id": film.movie_id,
            "verdict": "spared",
            "reason": "owns three of the collection",
            "decided_at": "2026-06-01T00:00:00+00:00",
            "reconsider_after": (NOW + timedelta(days=30)).isoformat(),
            "fact_fingerprint": fingerprint,
        }
        result = run(film, viewing=viewing, ctx=context(decisions={film.movie_id: decision}))
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("already spared", result.reasons[0])

    def test_a_changed_fact_reopens_it(self):
        film = make_film()
        decision = {
            "movie_id": film.movie_id,
            "verdict": "spared",
            "reason": "no completions at the time",
            "reconsider_after": (NOW + timedelta(days=30)).isoformat(),
            "fact_fingerprint": "stale-fingerprint",
        }
        result = run(film, ctx=context(decisions={film.movie_id: decision}))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_an_expired_decision_reopens_it(self):
        film = make_film()
        viewing = make_viewing()
        decision = {
            "movie_id": film.movie_id,
            "verdict": "spared",
            "reason": "old call",
            "reconsider_after": (NOW - timedelta(days=1)).isoformat(),
            "fact_fingerprint": fact_fingerprint(film, feed_provenance(), viewing, False),
        }
        result = run(film, viewing=viewing, ctx=context(decisions={film.movie_id: decision}))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_dismissal_is_not_reopened_by_the_same_evidence(self):
        film = make_film(tags={"cleanup-dismissed", "src-tmdb-popular"})
        viewing = make_viewing()
        decision = {
            "movie_id": film.movie_id,
            "verdict": "dismissed",
            "fact_fingerprint": fact_fingerprint(film, feed_provenance(), viewing, False),
        }
        result = run(film, viewing=viewing, ctx=context(decisions={film.movie_id: decision}))
        self.assertIs(result.outcome, Outcome.REVIEW)

    def test_decision_record_carries_a_reconsideration_date(self):
        record = build_decision_record(run(), "spared", "cult standing", "run-1", NOW, False)
        self.assertIn("reconsider_after", record)
        self.assertIn("fact_fingerprint", record)


class BatchTests(unittest.TestCase):
    """The cap is a limit, not a reason to abort."""

    def test_large_backlog_still_acts_on_the_cap(self):
        approved = [
            run(make_film(movie_id=i, size_bytes=i * 1_000_000_000)) for i in range(1, 81)
        ]
        selected, deferred = select_batch(approved)
        self.assertEqual(len(selected), MAX_DELETIONS_PER_RUN)
        self.assertEqual(len(deferred), 50)

    def test_largest_first(self):
        approved = [run(make_film(movie_id=i, size_bytes=i)) for i in (1, 99, 50)]
        selected, _ = select_batch(approved, cap=2)
        self.assertEqual([a.film.movie_id for a in selected], [99, 50])

    def test_a_concurrent_run_consumes_the_same_budget(self):
        approved = [run(make_film(movie_id=i, size_bytes=i)) for i in range(1, 41)]
        selected, deferred = select_batch(approved, already_deleted_this_window=28)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(deferred), 38)

    def test_budget_already_spent_selects_nothing(self):
        approved = [run(make_film(movie_id=i, size_bytes=i)) for i in range(1, 5)]
        selected, deferred = select_batch(approved, already_deleted_this_window=30)
        self.assertEqual(selected, [])
        self.assertEqual(len(deferred), 4)


class AnomalyTests(unittest.TestCase):
    """Volume is not an anomaly; a population that collapsed is."""

    BASE = {
        "library_size": 5000,
        "keep_tag_count": 4000,
        "collection_count": 700,
        "monitored_collections": 33,
        "exclusion_count": 2500,
        "history_rows": 500,
        "history_ok": True,
        "collections_loaded": True,
        "invalid_added_count": 0,
    }

    def test_no_baseline_blocks_and_bootstraps(self):
        blocks = check_anomalies(self.BASE, None)
        self.assertEqual(len(blocks), 1)
        self.assertIn("no validated baseline", blocks[0])

    def test_ordinary_run_is_not_blocked(self):
        self.assertEqual(check_anomalies({**self.BASE, "library_size": 4990}, self.BASE), [])

    def test_library_collapse_blocks(self):
        blocks = check_anomalies({**self.BASE, "library_size": 3000}, self.BASE)
        self.assertTrue(any("library size" in b for b in blocks))

    def test_lost_keep_tags_block(self):
        blocks = check_anomalies({**self.BASE, "keep_tag_count": 3800}, self.BASE)
        self.assertTrue(any("keep-tagged" in b for b in blocks))

    def test_history_reset_blocks(self):
        blocks = check_anomalies({**self.BASE, "history_rows": 100}, self.BASE)
        self.assertTrue(any("play-history" in b for b in blocks))

    def test_all_collections_unmonitored_blocks(self):
        blocks = check_anomalies({**self.BASE, "monitored_collections": 0}, self.BASE)
        self.assertTrue(any("monitored flag" in b for b in blocks))

    def test_many_invalid_dates_block(self):
        blocks = check_anomalies({**self.BASE, "invalid_added_count": 200}, self.BASE)
        self.assertTrue(any("unusable added date" in b for b in blocks))

    def test_growth_never_blocks(self):
        self.assertEqual(check_anomalies({**self.BASE, "library_size": 9000}, self.BASE), [])

    def test_malformed_snapshot_is_refused_as_a_baseline(self):
        problems = snapshot_valid({**self.BASE, "library_size": 0})
        self.assertTrue(problems)

    def test_snapshot_from_a_sick_history_service_is_refused(self):
        self.assertTrue(snapshot_valid({**self.BASE, "history_ok": False}))

    def test_good_snapshot_is_accepted(self):
        self.assertEqual(snapshot_valid(self.BASE), [])


if __name__ == "__main__":
    unittest.main()
