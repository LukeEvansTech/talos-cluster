"""Scope, protection, batching and anomaly rules."""

from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Any

from curatortv.model import HistoryStatus, Origin, Outcome
from curatortv.planner import (
    DEFAULT_JUDGE_LIMIT,
    MAX_DELETIONS_PER_RUN,
    PlanContext,
    assess,
    build_decision_record,
    check_anomalies,
    fact_fingerprint,
    keep_tag_applies,
    keep_tag_targets,
    select_batch,
    select_for_judgement,
    snapshot_valid,
)

from .fixtures import (
    NOW,
    SCOPE_START,
    days_ago,
    feed_provenance,
    make_availability,
    make_show,
    make_viewing,
    requested_provenance,
    unknown_provenance,
)


def context(**overrides) -> PlanContext:
    """A plan context with sane defaults for the fixed test clock."""
    base: dict[str, Any] = {
        "now": NOW,
        "scope_start": SCOPE_START,
        "coverage_start": days_ago(500),
        "history_ok": True,
        "decisions": {},
    }
    base.update(overrides)
    return PlanContext(**base)


def run(show=None, provenance=None, availability=None, viewing=None, ctx=None):
    """Assess one series with fixture defaults."""
    return assess(
        show or make_show(),
        provenance or feed_provenance(),
        availability or make_availability(),
        viewing or make_viewing(),
        ctx or context(),
    )


class ProtectionTests(unittest.TestCase):
    """Vetoes are checked before anything else and are absolute."""

    def test_keep_tag_protects(self):
        self.assertIs(run(make_show(tags={"keep"})).outcome, Outcome.PROTECTED)

    def test_keep_review_tag_protects(self):
        self.assertIs(run(make_show(tags={"keep-review"})).outcome, Outcome.PROTECTED)

    def test_human_keep_tag_protects(self):
        self.assertIs(run(make_show(tags={"cleanup-keep"})).outcome, Outcome.PROTECTED)

    def test_a_request_is_not_a_protection(self):
        """A requested series past its window goes to the judge, with who asked on its record."""
        result = run(make_show(tags=frozenset()), provenance=requested_provenance("Sam"))
        self.assertIs(result.outcome, Outcome.CANDIDATE)
        self.assertEqual(result.protections, [])
        self.assertEqual(result.to_json()["origin"], "requested")
        self.assertEqual(result.to_json()["requested_by"], "Sam")

    def test_a_requested_series_still_waits_out_its_window(self):
        result = run(provenance=requested_provenance(), availability=make_availability(days=100))
        self.assertIs(result.outcome, Outcome.NOT_DUE)

    def test_a_requested_series_over_the_rating_bar_is_still_protected(self):
        show = make_show(imdb_score=7.2, imdb_votes=80000)
        self.assertIs(run(show, provenance=requested_provenance()).outcome, Outcome.PROTECTED)


class RealityTests(unittest.TestCase):
    """A rating says how well a reality series is made, not whether anyone here wants it."""

    BAR = {"imdb_score": 7.8, "imdb_votes": 40000}

    def test_the_rating_bar_protects_a_scripted_series(self):
        self.assertIs(run(make_show(genres=("Drama",), **self.BAR)).outcome, Outcome.PROTECTED)

    def test_the_rating_bar_does_not_protect_a_reality_series(self):
        result = run(make_show(genres=("Reality", "Game Show"), **self.BAR))
        self.assertIs(result.outcome, Outcome.CANDIDATE)
        self.assertEqual(result.protections, [])

    def test_the_genre_is_matched_whatever_its_case(self):
        self.assertIs(run(make_show(genres=("reality",), **self.BAR)).outcome, Outcome.CANDIDATE)

    def test_a_reality_series_is_never_a_keep_tag_target(self):
        reality = make_show(series_id=2, tags=frozenset(), genres=("Reality",), **self.BAR)
        scripted = make_show(series_id=3, tags=frozenset(), genres=("Drama",), **self.BAR)
        self.assertEqual(keep_tag_targets([reality, scripted]), [scripted])

    def test_a_reality_series_already_carrying_keep_stays_vetoed(self):
        show = make_show(tags={"keep"}, genres=("Reality",), **self.BAR)
        self.assertIs(run(show).outcome, Outcome.PROTECTED)

    def test_a_judge_keep_may_still_be_tagged_on_a_reality_series(self):
        """Only the rating bar is switched off; a keep the judge hands down is tagged like any other."""
        self.assertTrue(keep_tag_applies(make_show(tags=frozenset(), genres=("Reality",), **self.BAR)))


class ScopeTests(unittest.TestCase):
    """Scope, windows and files."""

    def test_upcoming_is_out_of_scope(self):
        self.assertIs(run(make_show(status="upcoming")).outcome, Outcome.OUT_OF_SCOPE)

    def test_continuing_is_in_scope(self):
        self.assertIs(run(make_show(status="continuing")).outcome, Outcome.CANDIDATE)

    def test_a_boundary_that_is_set_still_puts_older_series_out_of_scope(self):
        self.assertIs(run(make_show(added=days_ago(400))).outcome, Outcome.OUT_OF_SCOPE)

    def test_with_no_boundary_the_whole_library_is_in_scope(self):
        result = run(make_show(added=days_ago(4000)), ctx=context(scope_start=None))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_unusable_added_date_goes_to_review(self):
        result = run(make_show(added=None))
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("added date", result.blockers[0])

    def test_missing_file_has_its_own_outcome(self):
        result = run(make_show(has_file=False))
        self.assertIs(result.outcome, Outcome.MISSING_FILE)

    def test_inside_the_window_is_not_due(self):
        result = run(availability=make_availability(days=100))
        self.assertIs(result.outcome, Outcome.NOT_DUE)
        self.assertEqual(result.window_days, 180)

    def test_low_score_uses_the_short_window(self):
        show = make_show(imdb_score=4.2)
        self.assertIs(run(show, availability=make_availability(days=70)).outcome, Outcome.CANDIDATE)
        self.assertIs(run(show, availability=make_availability(days=50)).outcome, Outcome.NOT_DUE)

    def test_a_new_season_restarts_the_window_of_a_series_somebody_has_played(self):
        """First import 300 days ago, newest season's first import 30 days ago, one play."""
        result = run(
            make_show(status="continuing"),
            availability=make_availability(days=300, window_days=30),
            viewing=make_viewing(plays=1),
        )
        self.assertIs(result.outcome, Outcome.NOT_DUE)
        self.assertEqual(result.days_available, 30)

    def test_an_airing_series_nobody_has_played_runs_from_its_first_import(self):
        """Left on the newest episode's clock, an airing show nobody watches never comes due."""
        result = run(make_show(status="continuing"), availability=make_availability(days=300, window_days=30))
        self.assertIs(result.outcome, Outcome.CANDIDATE)
        self.assertEqual(result.days_available, 300)
        self.assertEqual(result.to_json()["window_start"], days_ago(300).isoformat())
        self.assertIn("no plays", result.availability.evidence[-1])

    def test_a_zero_that_cannot_be_trusted_keeps_the_new_season_window(self):
        for status in (HistoryStatus.INCOMPLETE, HistoryStatus.UNRESOLVED, HistoryStatus.UNAVAILABLE):
            with self.subTest(status=status):
                result = run(
                    make_show(status="continuing"),
                    availability=make_availability(days=300, window_days=30),
                    viewing=make_viewing(status=status),
                )
                self.assertIs(result.outcome, Outcome.NOT_DUE)
                self.assertEqual(result.days_available, 30)

    def test_an_unplayed_airing_series_still_gets_its_first_window(self):
        result = run(make_show(status="continuing"), availability=make_availability(days=100, window_days=10))
        self.assertIs(result.outcome, Outcome.NOT_DUE)
        self.assertEqual(result.days_available, 100)


class EvidenceGateTests(unittest.TestCase):
    """A zero play count only counts when it is trustworthy."""

    def test_unavailable_history_blocks(self):
        self.assertIs(run(viewing=make_viewing(status=HistoryStatus.UNAVAILABLE)).outcome, Outcome.REVIEW)

    def test_incomplete_history_blocks(self):
        self.assertIs(run(viewing=make_viewing(status=HistoryStatus.INCOMPLETE)).outcome, Outcome.REVIEW)

    def test_unresolved_identity_blocks(self):
        self.assertIs(run(viewing=make_viewing(status=HistoryStatus.UNRESOLVED)).outcome, Outcome.REVIEW)

    def test_ambiguous_identity_blocks(self):
        self.assertIs(run(viewing=make_viewing(status=HistoryStatus.AMBIGUOUS)).outcome, Outcome.REVIEW)

    def test_two_viewers_protect(self):
        """Watched by the house is a protection, not a queue for a person."""
        result = run(viewing=make_viewing(viewers=2))
        self.assertIs(result.outcome, Outcome.PROTECTED)
        self.assertIn("2 distinct users", result.protections[0])
        self.assertEqual(result.blockers, [])

    def test_two_viewers_on_history_that_cannot_be_trusted_still_go_to_a_person(self):
        """The count protects only once the history behind it has been shown to be sound."""
        result = run(viewing=make_viewing(viewers=2, status=HistoryStatus.AMBIGUOUS))
        self.assertIs(result.outcome, Outcome.REVIEW)

    def test_two_viewers_do_not_outrank_the_window(self):
        result = run(viewing=make_viewing(viewers=2), availability=make_availability(days=100))
        self.assertIs(result.outcome, Outcome.NOT_DUE)

    def test_one_viewer_does_not_protect(self):
        self.assertIs(run(viewing=make_viewing(viewers=1, episodes=20)).outcome, Outcome.CANDIDATE)


class OriginTests(unittest.TestCase):
    """How a series arrived is on its record for the judge; it is not a permission."""

    def test_a_feed_series_is_a_candidate(self):
        result = run(provenance=feed_provenance())
        self.assertIs(result.outcome, Outcome.CANDIDATE)
        self.assertEqual(result.to_json()["origin"], "feed")

    def test_unknown_origin_is_a_candidate_not_a_queue(self):
        """No feed tag and no request used to park a series in front of a person for good."""
        result = run(make_show(tags=frozenset()), provenance=unknown_provenance())
        self.assertIs(result.outcome, Outcome.CANDIDATE)
        self.assertEqual(result.blockers, [])
        self.assertEqual(result.to_json()["origin"], "unknown")

    def test_the_eligible_tag_only_switches_off_the_rating_bar(self):
        over_the_bar = {"imdb_score": 7.4, "imdb_votes": 90000}
        self.assertIs(run(make_show(tags=frozenset(), **over_the_bar)).outcome, Outcome.PROTECTED)
        result = run(make_show(tags={"cleanup-eligible"}, **over_the_bar), provenance=unknown_provenance())
        self.assertIs(result.outcome, Outcome.CANDIDATE)
        self.assertIs(result.provenance.origin, Origin.UNKNOWN)

    def test_the_eligible_tag_changes_nothing_below_the_bar(self):
        with_tag = run(make_show(tags={"cleanup-eligible"}), provenance=unknown_provenance())
        without = run(make_show(tags=frozenset()), provenance=unknown_provenance())
        self.assertIs(with_tag.outcome, Outcome.CANDIDATE)
        self.assertEqual(with_tag.reasons, without.reasons)

    def test_the_judge_record_carries_the_original_language(self):
        self.assertEqual(run(make_show(original_language="Hindi")).to_json()["original_language"], "Hindi")

    def test_human_eligible_still_loses_to_a_veto(self):
        show = make_show(tags={"cleanup-eligible", "keep"})
        self.assertIs(run(show, provenance=unknown_provenance()).outcome, Outcome.PROTECTED)


class StandingDecisionTests(unittest.TestCase):
    """A settled judgement should not be re-litigated every week."""

    def test_a_binding_spare_suppresses_the_candidate(self):
        show, viewing = make_show(), make_viewing()
        decision = {
            "series_id": show.series_id,
            "verdict": "spared",
            "reason": "well-known completed series",
            "decided_at": "2026-06-01T00:00:00+00:00",
            "reconsider_after": (NOW + timedelta(days=30)).isoformat(),
            "fact_fingerprint": fact_fingerprint(show, feed_provenance(), viewing),
        }
        result = run(show, viewing=viewing, ctx=context(decisions={show.series_id: decision}))
        self.assertIs(result.outcome, Outcome.SPARED)
        self.assertIn("already spared", result.reasons[0])

    def test_a_review_the_judge_asked_for_stays_in_front_of_a_person(self):
        """A spare is settled. A review is not, until somebody acts on it."""
        show, viewing = make_show(), make_viewing()
        decision = {
            "series_id": show.series_id,
            "verdict": "queued for review",
            "reason": "does not recognise the series",
            "decided_at": "2026-06-01T00:00:00+00:00",
            "reconsider_after": (NOW + timedelta(days=30)).isoformat(),
            "fact_fingerprint": fact_fingerprint(show, feed_provenance(), viewing),
        }
        result = run(show, viewing=viewing, ctx=context(decisions={show.series_id: decision}))
        self.assertIs(result.outcome, Outcome.REVIEW)
        self.assertIn("already queued for review", result.reasons[0])

    def test_a_changed_fact_reopens_it(self):
        show = make_show()
        decision = {
            "series_id": show.series_id,
            "verdict": "spared",
            "reconsider_after": (NOW + timedelta(days=30)).isoformat(),
            "fact_fingerprint": "stale-fingerprint",
        }
        self.assertIs(run(show, ctx=context(decisions={show.series_id: decision})).outcome, Outcome.CANDIDATE)

    def test_a_show_ending_changes_the_fingerprint(self):
        viewing = make_viewing()
        self.assertNotEqual(
            fact_fingerprint(make_show(status="continuing"), feed_provenance(), viewing),
            fact_fingerprint(make_show(status="ended"), feed_provenance(), viewing),
        )

    def test_an_expired_decision_reopens_it(self):
        show, viewing = make_show(), make_viewing()
        decision = {
            "series_id": show.series_id,
            "verdict": "spared",
            "reconsider_after": (NOW - timedelta(days=1)).isoformat(),
            "fact_fingerprint": fact_fingerprint(show, feed_provenance(), viewing),
        }
        result = run(show, viewing=viewing, ctx=context(decisions={show.series_id: decision}))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_dismissal_is_not_reopened_by_the_same_evidence(self):
        show = make_show(tags={"cleanup-dismissed", "kometa"})
        viewing = make_viewing()
        decision = {
            "series_id": show.series_id,
            "verdict": "dismissed",
            "fact_fingerprint": fact_fingerprint(show, feed_provenance(), viewing),
        }
        result = run(show, viewing=viewing, ctx=context(decisions={show.series_id: decision}))
        self.assertIs(result.outcome, Outcome.SPARED)

    def test_a_dismissal_answers_a_review_the_judge_asked_for(self):
        """Tagging it dismissed is how a person replies; it must not stay listed as waiting."""
        show = make_show(tags={"cleanup-dismissed", "kometa"})
        viewing = make_viewing()
        decision = {
            "series_id": show.series_id,
            "verdict": "queued for review",
            "reason": "does not recognise it",
            "decided_at": "2026-06-01T00:00:00+00:00",
            "reconsider_after": (NOW + timedelta(days=30)).isoformat(),
            "fact_fingerprint": fact_fingerprint(show, feed_provenance(), viewing),
        }
        result = run(show, viewing=viewing, ctx=context(decisions={show.series_id: decision}))
        self.assertIs(result.outcome, Outcome.SPARED)
        self.assertIn("dismissed by a person", result.reasons[0])

    def test_a_dismissal_after_a_review_is_still_reopened_by_a_changed_fact(self):
        show = make_show(tags={"cleanup-dismissed", "kometa"})
        decision = {
            "series_id": show.series_id,
            "verdict": "queued for review",
            "reconsider_after": (NOW + timedelta(days=30)).isoformat(),
            "fact_fingerprint": "the-facts-as-they-were",
        }
        result = run(show, ctx=context(decisions={show.series_id: decision}))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_decision_record_carries_a_reconsideration_date(self):
        record = build_decision_record(run(), "spared", "cult standing", "run-1", NOW)
        self.assertIn("reconsider_after", record)
        self.assertIn("fact_fingerprint", record)
        self.assertEqual(record["series_id"], 1)


class BatchTests(unittest.TestCase):
    """The cap is a limit, not a reason to abort."""

    def test_large_backlog_still_acts_on_the_cap(self):
        approved = [run(make_show(series_id=i, size_bytes=i * 1_000_000_000)) for i in range(1, 41)]
        selected, deferred = select_batch(approved)
        self.assertEqual(len(selected), MAX_DELETIONS_PER_RUN)
        self.assertEqual(MAX_DELETIONS_PER_RUN, 10)
        self.assertEqual(len(deferred), 30)

    def test_largest_first(self):
        approved = [run(make_show(series_id=i, size_bytes=i)) for i in (1, 99, 50)]
        selected, _ = select_batch(approved, cap=2)
        self.assertEqual([a.show.series_id for a in selected], [99, 50])


class JudgeLimitTests(unittest.TestCase):
    """The judge is asked about a bounded number of candidates; the rest wait."""

    def candidates(self, count: int):
        """``count`` candidates whose size rises with their id."""
        return [run(make_show(series_id=i, size_bytes=i * 1_000_000_000)) for i in range(1, count + 1)]

    def test_the_largest_are_offered_and_the_rest_deferred(self):
        offered, deferred = select_for_judgement(self.candidates(10), limit=3)
        self.assertEqual([a.show.series_id for a in offered], [10, 9, 8])
        self.assertEqual([a.show.series_id for a in deferred], [7, 6, 5, 4, 3, 2, 1])

    def test_a_backlog_under_the_limit_is_offered_whole(self):
        offered, deferred = select_for_judgement(self.candidates(4), limit=30)
        self.assertEqual(len(offered), 4)
        self.assertEqual(deferred, [])

    def test_the_default_limit(self):
        offered, deferred = select_for_judgement(self.candidates(DEFAULT_JUDGE_LIMIT + 5))
        self.assertEqual(len(offered), 30)
        self.assertEqual(len(deferred), 5)

    def test_a_concurrent_run_consumes_the_same_budget(self):
        approved = [run(make_show(series_id=i, size_bytes=i)) for i in range(1, 21)]
        selected, deferred = select_batch(approved, already_deleted_this_window=8)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(deferred), 18)

    def test_budget_already_spent_selects_nothing(self):
        approved = [run(make_show(series_id=i, size_bytes=i)) for i in range(1, 5)]
        selected, deferred = select_batch(approved, already_deleted_this_window=10)
        self.assertEqual(selected, [])
        self.assertEqual(len(deferred), 4)


class AnomalyTests(unittest.TestCase):
    """Volume is not an anomaly; a population that collapsed is."""

    BASE = {
        "library_size": 900,
        "keep_tag_count": 400,
        "exclusion_count": 40,
        "history_rows": 50000,
        "history_ok": True,
        "invalid_added_count": 0,
    }

    def test_no_baseline_blocks_and_bootstraps(self):
        blocks = check_anomalies(self.BASE, None)
        self.assertEqual(len(blocks), 1)
        self.assertIn("no validated baseline", blocks[0])

    def test_ordinary_run_is_not_blocked(self):
        self.assertEqual(check_anomalies({**self.BASE, "library_size": 890}, self.BASE), [])

    def test_library_collapse_blocks(self):
        blocks = check_anomalies({**self.BASE, "library_size": 500}, self.BASE)
        self.assertTrue(any("library size" in b for b in blocks))

    def test_lost_keep_tags_block(self):
        blocks = check_anomalies({**self.BASE, "keep_tag_count": 380}, self.BASE)
        self.assertTrue(any("keep-tagged" in b for b in blocks))

    def test_lost_exclusions_block(self):
        blocks = check_anomalies({**self.BASE, "exclusion_count": 30}, self.BASE)
        self.assertTrue(any("import exclusions" in b for b in blocks))

    def test_history_reset_blocks(self):
        blocks = check_anomalies({**self.BASE, "history_rows": 100}, self.BASE)
        self.assertTrue(any("play-history" in b for b in blocks))

    def test_many_invalid_dates_block(self):
        blocks = check_anomalies({**self.BASE, "invalid_added_count": 50}, self.BASE)
        self.assertTrue(any("unusable added date" in b for b in blocks))

    def test_growth_never_blocks(self):
        self.assertEqual(check_anomalies({**self.BASE, "library_size": 9000}, self.BASE), [])

    def test_malformed_snapshot_is_refused_as_a_baseline(self):
        self.assertTrue(snapshot_valid({**self.BASE, "library_size": 0}))

    def test_snapshot_from_a_sick_history_service_is_refused(self):
        self.assertTrue(snapshot_valid({**self.BASE, "history_ok": False}))

    def test_good_snapshot_is_accepted(self):
        self.assertEqual(snapshot_valid(self.BASE), [])


if __name__ == "__main__":
    unittest.main()
