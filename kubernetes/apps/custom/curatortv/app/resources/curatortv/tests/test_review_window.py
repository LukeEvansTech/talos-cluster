"""The review window: a verdict waits where a person can overrule it before anything is done.

Covers what the planner does with a verdict on record, a person's
``cleanup-eligible`` delete, the baseline an operator accepts, the document the
review page is sent, and what the digest says about all of it.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from curatortv import __main__ as cli
from curatortv import proposals, reporting, review
from curatortv.acting import adopt_baseline, due_deletes
from curatortv.ledger import Ledger
from curatortv.model import HistoryStatus, Outcome
from curatortv.planner import (
    CAP_WINDOW_DAYS,
    accepted_baseline,
    assess,
    build_decision_record,
    lapsed_proposals,
    select_batch,
)

from .fixtures import (
    NOW,
    FakeS3,
    days_ago,
    feed_provenance,
    make_availability,
    make_show,
    make_viewing,
)
from .test_executor import series_record
from .test_judge_limit import RUN
from .test_planner import context, run

REASON = "trending add nobody watched"
WEEK_AGO = NOW - timedelta(days=7)


def on_record(show=None, verdict: str = "delete", viewing=None, made: datetime = WEEK_AGO) -> dict:
    """The record an earlier run would have left after judging this series."""
    judged = run(show, viewing=viewing)
    entry = {**judged.to_json(), "proposal": proposals.judged(verdict, REASON, made, 6)}
    return proposals.record(entry, "series_id", "run-earlier")


def with_record(record: dict, **overrides):
    """A plan context that has one proposal on record."""
    return context(proposals={record["series_id"]: record}, **overrides)


class StandingProposalTests(unittest.TestCase):
    """A title with a verdict on record is not judged again while its facts hold."""

    def test_a_series_with_a_delete_on_record_is_proposed_not_a_candidate(self):
        result = run(ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.PROPOSED)

    def test_the_plan_entry_carries_the_verdict_and_when_it_falls_due(self):
        record = on_record()
        entry = run(ctx=with_record(record)).to_json()
        self.assertEqual(entry["proposal"]["verdict"], "delete")
        self.assertEqual(entry["proposal"]["by"], "judge")
        self.assertEqual(entry["proposal"]["due_at"], (WEEK_AGO + timedelta(days=6)).isoformat())
        self.assertEqual(entry["proposal"]["reason"], REASON)

    def test_a_keep_on_record_is_proposed_too(self):
        result = run(ctx=with_record(on_record(verdict="keep")))
        self.assertIs(result.outcome, Outcome.PROPOSED)
        self.assertEqual(result.proposal["verdict"], "keep")

    def test_without_a_record_the_same_series_is_a_candidate(self):
        self.assertIs(run().outcome, Outcome.CANDIDATE)

    def test_somebody_finishing_it_lapses_the_proposal(self):
        result = run(viewing=make_viewing(viewers=1), ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_somebody_starting_it_lapses_the_proposal(self):
        """A play that did not finish leaves the fingerprint alone, and is still a reason to ask again."""
        result = run(viewing=make_viewing(plays=1), ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_a_new_season_lapses_the_proposal(self):
        result = run(make_show(season_count=2), ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.CANDIDATE)

    def test_a_rating_drifting_inside_its_bucket_does_not(self):
        result = run(make_show(imdb_score=6.1, imdb_votes=4100), ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.PROPOSED)

    def test_cleanup_keep_drops_the_proposal_and_protects(self):
        result = run(make_show(tags={"cleanup-keep"}), ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.PROTECTED)
        self.assertIsNone(result.proposal)

    def test_a_dismissal_outranks_the_proposal(self):
        result = run(make_show(tags={"cleanup-dismissed"}), ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.SPARED)

    def test_a_protection_that_now_applies_outranks_the_proposal(self):
        cases = {
            "the rating bar": (make_show(imdb_score=7.4, imdb_votes=90000), None),
            "two viewers": (None, make_viewing(viewers=2)),
        }
        for name, (show, viewing) in cases.items():
            with self.subTest(name):
                result = run(show, viewing=viewing, ctx=with_record(on_record()))
                self.assertIs(result.outcome, Outcome.PROTECTED)

    def test_evidence_that_can_no_longer_be_trusted_stops_the_proposal(self):
        """A delete on record is not carried out on a play count the engine would not act on today."""
        result = run(viewing=make_viewing(status=HistoryStatus.AMBIGUOUS), ctx=with_record(on_record()))
        self.assertIs(result.outcome, Outcome.REVIEW)

    def test_a_record_that_cannot_be_read_does_not_stand(self):
        for field, value in (("due_at", "soon"), ("verdict", "review"), ("by", "somebody"), ("fact_fingerprint", "")):
            with self.subTest(field):
                record = {**on_record(), field: value}
                self.assertIs(run(ctx=with_record(record)).outcome, Outcome.CANDIDATE)

    def test_a_binding_spare_is_still_reported_as_spared(self):
        """The two records never coexist in practice; if they do, the settled one wins."""
        record = on_record()
        judged = run()
        spare = build_decision_record(judged, "spared", "cult standing", "run-0", NOW)
        result = run(ctx=context(proposals={1: record}, decisions={1: spare}))
        self.assertIs(result.outcome, Outcome.SPARED)


class PersonDeleteTests(unittest.TestCase):
    """``cleanup-eligible`` is a person saying this goes: it takes the judge's place, not the gates'."""

    ELIGIBLE = frozenset({"cleanup-eligible"})

    def test_it_becomes_a_delete_by_a_person_due_at_once(self):
        result = run(make_show(tags=self.ELIGIBLE))
        self.assertIs(result.outcome, Outcome.PROPOSED)
        self.assertEqual(result.proposal["by"], "person")
        self.assertEqual(result.proposal["verdict"], "delete")
        self.assertTrue(proposals.is_due(result.proposal, NOW))

    def test_it_overrides_the_rating_bar(self):
        result = run(make_show(tags=self.ELIGIBLE, imdb_score=7.4, imdb_votes=90000))
        self.assertIs(result.outcome, Outcome.PROPOSED)

    def test_it_overrides_a_keep_the_judge_proposed(self):
        record = on_record(verdict="keep")
        result = run(make_show(tags=self.ELIGIBLE), ctx=with_record(record))
        self.assertEqual((result.proposal["verdict"], result.proposal["by"]), ("delete", "person"))

    def test_it_overrides_a_spare_and_a_review_the_judge_recorded(self):
        for verdict in ("spared", "queued for review"):
            with self.subTest(verdict):
                decision = build_decision_record(run(), verdict, "cult standing", "run-0", NOW)
                result = run(make_show(tags=self.ELIGIBLE), ctx=context(decisions={1: decision}))
                self.assertIs(result.outcome, Outcome.PROPOSED)

    def test_it_does_not_override_the_allow_list(self):
        for tag in ("keep", "cleanup-keep", "keep-review"):
            with self.subTest(tag):
                result = run(make_show(tags={"cleanup-eligible", tag}))
                self.assertIs(result.outcome, Outcome.PROTECTED)

    def test_it_does_not_let_a_reality_series_skip_the_gates_either(self):
        """The bar never applied to it, so the tag has nothing to switch off; the window still holds."""
        show = make_show(tags=self.ELIGIBLE, genres=("Reality",), imdb_score=7.4, imdb_votes=90000)
        self.assertIs(run(show, availability=make_availability(days=10)).outcome, Outcome.NOT_DUE)
        self.assertIs(run(show).outcome, Outcome.PROPOSED)

    def test_it_does_not_shorten_the_viewing_window(self):
        result = run(make_show(tags=self.ELIGIBLE), availability=make_availability(days=10))
        self.assertIs(result.outcome, Outcome.NOT_DUE)

    def test_it_does_not_override_a_series_the_house_watched(self):
        result = run(make_show(tags=self.ELIGIBLE), viewing=make_viewing(viewers=2))
        self.assertIs(result.outcome, Outcome.PROTECTED)

    def test_it_does_not_override_evidence_that_cannot_be_trusted(self):
        result = run(make_show(tags=self.ELIGIBLE), viewing=make_viewing(status=HistoryStatus.INCOMPLETE))
        self.assertIs(result.outcome, Outcome.REVIEW)

    def test_a_dismissal_beside_it_spares_the_film(self):
        """Two tags from a person that disagree: the one that deletes nothing wins."""
        result = run(make_show(tags={"cleanup-eligible", "cleanup-dismissed"}))
        self.assertIs(result.outcome, Outcome.SPARED)

    def test_a_reopened_dismissal_beside_it_still_stops_the_persons_delete(self):
        """A changed fact reopens a dismissal for the judge, not for an immediate delete."""
        old = {"verdict": "spared", "fact_fingerprint": "facts-have-moved", "decided_at": WEEK_AGO.isoformat()}
        result = run(make_show(tags={"cleanup-eligible", "cleanup-dismissed"}), ctx=context(decisions={1: old}))
        self.assertIsNot(result.outcome, Outcome.PROPOSED)
        self.assertIsNone(result.proposal)

    def test_one_already_on_record_keeps_its_dates(self):
        # Far enough past its window to have been due a week ago as well.
        first = run(make_show(tags=self.ELIGIBLE), availability=make_availability(days=200), ctx=context(now=WEEK_AGO))
        record = proposals.record(first.to_json(), "series_id", "run-earlier")
        again = run(make_show(tags=self.ELIGIBLE), ctx=with_record(record))
        self.assertEqual(again.proposal["proposed_at"], WEEK_AGO.isoformat())

    def test_execute_rechecks_the_tag_on_each_persons_delete(self):
        """Its delete is marked so the live re-check right before the delete requires the tag again."""
        entry = run(make_show(tags=self.ELIGIBLE)).to_json()
        approved, set_aside, marked = due_deletes([entry], {}, {1: series_record(tags=[9])}, {9: "cleanup-eligible"})
        self.assertEqual((set_aside, sorted(marked)), ([], [1]))
        self.assertTrue(approved[0].marked_by_person)

    def test_taking_the_tag_off_hands_the_series_back_to_the_judge(self):
        first = run(make_show(tags=self.ELIGIBLE))
        record = proposals.record(first.to_json(), "series_id", "run-earlier")
        self.assertIs(run(ctx=with_record(record)).outcome, Outcome.CANDIDATE)


class LapsedProposalTests(unittest.TestCase):
    """The plan names every proposal it no longer upholds, so execute can take it off the record."""

    def test_an_upheld_proposal_is_not_listed(self):
        record = on_record()
        self.assertEqual(lapsed_proposals({1: record}, [run(ctx=with_record(record))]), [])

    def test_a_kept_series_is_listed_with_why(self):
        record = on_record()
        assessed = run(make_show(tags={"cleanup-keep"}), ctx=with_record(record))
        lapsed = lapsed_proposals({1: record}, [assessed])
        self.assertEqual(lapsed[0]["series_id"], 1)
        self.assertEqual(lapsed[0]["why"], "now protected: carries cleanup-keep")

    def test_a_series_that_was_played_is_listed_as_a_candidate_again(self):
        record = on_record()
        assessed = run(viewing=make_viewing(viewers=1), ctx=with_record(record))
        self.assertTrue(lapsed_proposals({1: record}, [assessed])[0]["why"].startswith("now candidate"))

    def test_a_series_that_left_the_library_is_listed(self):
        self.assertEqual(lapsed_proposals({1: on_record()}, [])[0]["why"], "no longer in the library")

    def test_a_judges_proposal_a_person_has_overridden_is_not_listed(self):
        """It is replaced on record, not removed, when execute writes the person's delete over it."""
        record = on_record(verdict="keep")
        assessed = run(make_show(tags={"cleanup-eligible"}), ctx=with_record(record))
        self.assertEqual(lapsed_proposals({1: record}, [assessed]), [])


class PlanPayloadTests(unittest.TestCase):
    """The judge limit is spent on titles with no verdict yet."""

    def payload(self, limit: int = 1) -> dict:
        """A plan over three series, the largest of which already has a delete on record."""
        shows = {
            n: make_show(series_id=n, tmdb_id=1000 * n, size_bytes=n * 10_000_000_000, tvdb_id=70000 + n)
            for n in (1, 2, 3)
        }
        record = on_record(shows[3])
        ctx = with_record(record)
        assessments = [
            assess(show, feed_provenance(), make_availability(), make_viewing(), ctx) for show in shows.values()
        ]
        run_facts = {**RUN, "judge_limit": limit, "lapsed": lapsed_proposals({3: record}, assessments)}
        return cli.build_plan_payload("run-plan", NOW, True, ctx, assessments, run_facts)

    def test_a_proposed_series_is_not_offered_to_the_judge(self):
        plan = self.payload()
        self.assertEqual([c["series_id"] for c in plan["candidates"]], [2])
        self.assertEqual([c["series_id"] for c in plan["judge_deferred"]], [1])

    def test_it_is_listed_with_its_proposal_instead(self):
        plan = self.payload()
        self.assertEqual([p["series_id"] for p in plan["proposed"]], [3])
        self.assertEqual(plan["proposed"][0]["proposal"]["verdict"], "delete")
        self.assertEqual(plan["counts"], {"candidate": 2, "proposed": 1})
        self.assertEqual(plan["lapsed_proposals"], [])

    def test_a_candidate_carries_the_fingerprint_its_verdict_will_rest_on(self):
        candidate = self.payload()["candidates"][0]
        self.assertEqual(len(candidate["fact_fingerprint"]), 16)
        self.assertIsNone(candidate["proposal"])


class ProposalRuleTests(unittest.TestCase):
    """The small rules everything above rests on."""

    def test_a_verdict_falls_due_after_its_wait(self):
        proposal = proposals.judged("delete", REASON, NOW, 6)
        self.assertFalse(proposals.is_due(proposal, NOW + timedelta(days=5, hours=23)))
        self.assertTrue(proposals.is_due(proposal, NOW + timedelta(days=6)))

    def test_the_default_wait_ends_before_the_next_weekly_run(self):
        """Recorded half an hour into one run, it has to be due when the next starts a week on."""
        proposal = proposals.judged("delete", REASON, NOW + timedelta(minutes=45), proposals.DEFAULT_GRACE_DAYS)
        self.assertTrue(proposals.is_due(proposal, proposals.next_run(NOW, NOW)))

    def test_a_proposal_with_no_usable_date_is_never_due(self):
        for value in (None, "", "next week", 12):
            with self.subTest(value=value):
                self.assertFalse(proposals.is_due({"due_at": value}, NOW))

    def test_a_plan_entry_is_backed_only_by_the_same_verdict_on_record(self):
        record = on_record()
        entry = {"series_id": 1, "proposal": proposals.summary(record)}
        self.assertTrue(proposals.backed(entry, record))
        self.assertFalse(proposals.backed(entry, None))
        self.assertFalse(proposals.backed(entry, {**record, "verdict": "keep"}))
        self.assertFalse(proposals.backed(entry, {**record, "due_at": NOW.isoformat()}))
        self.assertFalse(proposals.backed(entry, {**record, "by": "person"}))

    def test_due_proposals_are_split_by_verdict_and_the_rest_left_out(self):
        entries = [
            {"series_id": 1, "proposal": proposals.judged("delete", REASON, WEEK_AGO, 6)},
            {"series_id": 2, "proposal": proposals.judged("keep", REASON, WEEK_AGO, 6)},
            {"series_id": 3, "proposal": proposals.judged("delete", REASON, NOW, 6)},
            {"series_id": 4},
        ]
        deletes, keeps = proposals.split_due(entries, NOW)
        self.assertEqual([e["series_id"] for e in deletes], [1])
        self.assertEqual([e["series_id"] for e in keeps], [2])

    def test_the_record_keeps_the_evidence_but_not_a_second_copy_of_the_proposal(self):
        record = on_record()
        self.assertNotIn("proposal", record["entry"])
        self.assertEqual(record["entry"]["title"], "Test Show")
        self.assertEqual(record["play_count"], 0)
        self.assertEqual(record["run_id"], "run-earlier")

    def test_a_persons_delete_goes_first_whatever_its_size(self):
        small = run(make_show(series_id=1, size_bytes=1_000_000_000))
        large = run(make_show(series_id=2, size_bytes=90_000_000_000))
        selected, deferred = select_batch([large, small], cap=1, first=frozenset({1}))
        self.assertEqual([a.show.series_id for a in selected], [1])
        self.assertEqual([a.show.series_id for a in deferred], [2])


class LedgerProposalTests(unittest.TestCase):
    """Proposals are durable, and a rehearsal's are kept apart from the real ones."""

    def test_a_proposal_is_read_back_by_id(self):
        ledger = Ledger(FakeS3(), "r1", dry_run=False)
        ledger.write_proposal(on_record())
        self.assertEqual(ledger.read_proposals()[1]["verdict"], "delete")

    def test_a_rehearsal_records_under_its_own_prefix(self):
        store = FakeS3()
        Ledger(store, "r1", dry_run=True).write_proposal(on_record())
        self.assertIn("dry-run/proposals/1.json", store.store)
        self.assertEqual(Ledger(store, "r2", dry_run=False).read_proposals(), {})

    def test_a_proposal_can_be_taken_off_the_record(self):
        ledger = Ledger(FakeS3(), "r1", dry_run=False)
        ledger.write_proposal(on_record())
        ledger.delete_proposal(1)
        self.assertEqual(ledger.read_proposals(), {})

    def test_a_later_verdict_replaces_the_earlier_one(self):
        ledger = Ledger(FakeS3(), "r1", dry_run=False)
        ledger.write_proposal(on_record())
        ledger.write_proposal(on_record(verdict="keep"))
        self.assertEqual(ledger.read_proposals()[1]["verdict"], "keep")

    def test_records_that_cannot_be_read_are_ignored_not_fatal(self):
        store = FakeS3()
        store.put("proposals/1.json", b"{ truncated")
        store.put("proposals/2.json", json.dumps(["not", "an", "object"]).encode())
        store.put("proposals/3.json", json.dumps({"series_id": True}).encode())
        self.assertEqual(Ledger(store, "r1", dry_run=False).read_proposals(), {})


def snapshot(taken: datetime, **overrides) -> dict:
    """A snapshot good enough to be a baseline."""
    base = {
        "library_size": 6000,
        "keep_tag_count": 1700,
        "collection_count": 400,
        "exclusion_count": 50,
        "history_rows": 9000,
        "history_ok": True,
        "taken_at": taken.isoformat(),
    }
    return {**base, **overrides}


class AcceptBaselineTests(unittest.TestCase):
    """An operator can accept the figures a blocked run reported, once."""

    def setUp(self):
        self.store = FakeS3()
        self.blocked = Ledger(self.store, "run-blocked", dry_run=False)
        self.blocked.write_baseline(snapshot(days_ago(14)))
        self.blocked.record_plan({"run_id": "run-blocked", "snapshot": snapshot(days_ago(7), keep_tag_count=1650)})
        self.ledger = Ledger(self.store, "run-now", dry_run=False)

    def test_a_named_runs_snapshot_becomes_the_baseline(self):
        baseline, note = adopt_baseline(self.ledger, "run-blocked", self.ledger.read_baseline())
        self.assertEqual(baseline["keep_tag_count"], 1650)
        self.assertEqual(baseline["accepted_from_run"], "run-blocked")
        self.assertIn("accepted the snapshot", note)

    def test_it_is_honoured_once_and_then_inert(self):
        """Left in the manifest it must not accept anything a later run finds."""
        adopt_baseline(self.ledger, "run-blocked", self.ledger.read_baseline())
        self.ledger.write_baseline(snapshot(NOW))
        baseline, note = adopt_baseline(self.ledger, "run-blocked", self.ledger.read_baseline())
        self.assertEqual(baseline["taken_at"], NOW.isoformat())
        self.assertIn("already as new", note)

    def test_a_run_that_is_not_on_record_changes_nothing(self):
        baseline, note = adopt_baseline(self.ledger, "run-missing", self.ledger.read_baseline())
        self.assertEqual(baseline["keep_tag_count"], 1700)
        self.assertIn("ignored", note)

    def test_a_snapshot_that_could_not_be_a_baseline_is_refused(self):
        self.blocked.record_plan({"snapshot": snapshot(days_ago(7), history_ok=False)})
        baseline, note = adopt_baseline(self.ledger, "run-blocked", self.ledger.read_baseline())
        self.assertEqual(baseline["keep_tag_count"], 1700)
        self.assertIn("cannot be a baseline", note)

    def test_a_value_that_is_not_a_run_id_is_never_used_as_a_path(self):
        for value in ("../../etc", "run/../../x", ".hidden", "a" * 200, ""):
            with self.subTest(value=value):
                baseline, note = adopt_baseline(self.ledger, value, {"kept": True})
                self.assertEqual(baseline, {"kept": True})
                self.assertIn("not a run id", note)

    def test_a_rehearsals_plan_is_not_accepted_into_the_real_record(self):
        Ledger(self.store, "run-dry", dry_run=True).record_plan({"snapshot": snapshot(days_ago(1))})
        baseline, _ = adopt_baseline(self.ledger, "run-dry", self.ledger.read_baseline())
        self.assertEqual(baseline["keep_tag_count"], 1700)

    def test_with_no_baseline_yet_the_named_snapshot_is_taken(self):
        accepted, _ = accepted_baseline({"snapshot": snapshot(days_ago(7))}, None)
        self.assertEqual(accepted["library_size"], 6000)

    def test_a_snapshot_with_no_date_is_refused(self):
        accepted, note = accepted_baseline({"snapshot": snapshot(NOW, taken_at="")}, None)
        self.assertIsNone(accepted)
        self.assertIn("taken_at", note)


class CapWindowTests(unittest.TestCase):
    """The cap is per weekly run: last week's deletions must not use up this week's budget."""

    def outcome(self, ledger: Ledger, store: FakeS3, series_id: int, age: timedelta) -> None:
        """A confirmed deletion recorded ``age`` ago."""
        ledger.record_outcome(series_id, "deleted", "HTTP 200, verified gone")
        key = f"runs/{ledger.run_id}/outcome/{series_id}.json"
        record = store.loads(key)
        record["recorded_at"] = (datetime.now(timezone.utc) - age).isoformat()
        store.put(key, json.dumps(record).encode())

    def test_a_deletion_from_last_weeks_run_is_outside_the_window(self):
        """Recorded minutes after the time this run counts from, a week earlier."""
        store = FakeS3()
        ledger = Ledger(store, "run-last-week", dry_run=False)
        self.outcome(ledger, store, 1, timedelta(days=7) - timedelta(minutes=10))
        self.assertEqual(ledger.deletions_in_window(CAP_WINDOW_DAYS), 0)
        self.assertEqual(ledger.deletions_in_window(7), 1)

    def test_a_second_run_in_the_same_week_still_counts_the_first(self):
        store = FakeS3()
        ledger = Ledger(store, "run-yesterday", dry_run=False)
        self.outcome(ledger, store, 1, timedelta(days=1))
        self.assertEqual(ledger.deletions_in_window(CAP_WINDOW_DAYS), 1)


def stored(series_id: int, verdict: str = "delete", size_gb: float = 10.0, **extra) -> dict:
    """A proposal on record, by the judge unless ``by`` says otherwise."""
    made = extra.pop("made", WEEK_AGO)
    by = extra.pop("by", "judge")
    proposal = proposals.judged(verdict, REASON, made, 6) if by == "judge" else proposals.by_person(None, made)
    entry = {
        "series_id": series_id,
        "title": f"Show {series_id}",
        "year": 2019,
        "size_gb": size_gb,
        "imdb_score": 4.9,
        "imdb_votes": 812,
        "play_count": 0,
        "distinct_viewers": 0,
        "origin": "feed",
        "requested_by": None,
        "overview": "A synopsis.",
        "fact_fingerprint": "abcdef0123456789",
        **extra,
        "proposal": proposal,
    }
    return proposals.record(entry, "series_id", "run-earlier")


def document(records: list[dict], plan: dict | None = None, result: dict | None = None, **run_facts) -> dict:
    """The review document for these proposals, as of the fixed test clock."""
    plan = {"run_id": "run-plan", "generated_at": NOW.isoformat(), **(plan or {})}
    facts = {"cap": 30, "mode": "act", "blocks": [], **run_facts}
    return review.build("tv", "series_id", plan, result, {r["series_id"]: r for r in records}, NOW, facts)


SATURDAYS = "30 9 * * 6"
LONDON = "Europe/London"


def at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """A UTC moment."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class RunStartTests(unittest.TestCase):
    """What "due" is measured against: the firing this run belongs to, not the clock."""

    def test_minutes_into_the_slot_it_is_the_slot(self):
        self.assertEqual(proposals.run_start(at(2026, 11, 28, 9, 33), SATURDAYS, LONDON), at(2026, 11, 28, 9, 30))

    def test_on_the_minute_it_is_that_minute(self):
        self.assertEqual(proposals.run_start(at(2026, 11, 28, 9, 30), SATURDAYS, LONDON), at(2026, 11, 28, 9, 30))

    def test_a_run_started_by_hand_midweek_is_measured_against_the_last_slot(self):
        self.assertEqual(proposals.run_start(at(2026, 12, 2, 14, 0), SATURDAYS, LONDON), at(2026, 11, 28, 9, 30))

    def test_the_slot_is_read_in_the_cronjobs_zone(self):
        """British Summer Time: 09:30 in London is 08:30 UTC."""
        self.assertEqual(proposals.run_start(at(2026, 7, 4, 8, 45), SATURDAYS, LONDON), at(2026, 7, 4, 8, 30))

    def test_a_verdict_falling_due_while_the_judge_answers_is_not_yet_due(self):
        proposal = {"due_at": at(2026, 11, 28, 9, 31).isoformat()}
        now = at(2026, 11, 28, 9, 33)
        self.assertTrue(proposals.is_due(proposal, now))
        self.assertFalse(proposals.is_due(proposal, proposals.run_start(now, SATURDAYS, LONDON)))

    def test_what_the_page_listed_as_going_is_what_the_run_takes(self):
        """The page measures against the next firing; the run it names measures against the same moment."""
        told = proposals.next_run(None, at(2026, 11, 22, 10, 5), SATURDAYS, LONDON)
        self.assertEqual(proposals.run_start(told + timedelta(minutes=4), SATURDAYS, LONDON), told)

    def test_without_a_schedule_it_is_the_clock(self):
        now = at(2026, 11, 28, 9, 33)
        self.assertEqual(proposals.run_start(now), now)
        self.assertEqual(proposals.run_start(now, "30 9 * * SAT", LONDON), now)


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

    def test_told_to_act_as_any_other_it_names_no_moment(self):
        started, why = self.read(CLEANUP_RUN_AS="2026-12-05T09:30:00Z")
        self.assertIsNone(started)
        self.assertIn("CLEANUP_RUN_AS", why)


class NextRunTests(unittest.TestCase):
    """The day a person is told: the CronJob's next firing, whenever this run started."""

    def test_a_run_started_by_hand_still_names_the_scheduled_day(self):
        """A Sunday run, the day after the scheduled one: the next is six days on, not seven."""
        sunday = at(2026, 11, 22, 10, 5)
        self.assertEqual(proposals.next_run(sunday, sunday, SATURDAYS, LONDON), at(2026, 11, 28, 9, 30))

    def test_a_run_inside_its_own_slot_names_next_weeks(self):
        started = at(2026, 11, 28, 9, 35)
        self.assertEqual(proposals.next_run(started, started, SATURDAYS, LONDON), at(2026, 12, 5, 9, 30))

    def test_a_run_just_ahead_of_the_slot_names_todays(self):
        self.assertEqual(proposals.next_firing(SATURDAYS, LONDON, at(2026, 11, 28, 9, 0)), at(2026, 11, 28, 9, 30))

    def test_the_schedule_is_read_in_the_cronjobs_time_zone(self):
        summer = at(2026, 7, 1, 12, 0)
        self.assertEqual(proposals.next_firing(SATURDAYS, LONDON, summer), at(2026, 7, 4, 8, 30))
        self.assertEqual(proposals.next_firing(SATURDAYS, "", summer), at(2026, 7, 4, 9, 30))

    def test_lists_ranges_steps_and_both_spellings_of_sunday_are_read(self):
        sunday = at(2026, 11, 22, 10, 5)
        cases = {
            "*/15 * * * *": at(2026, 11, 22, 10, 15),
            "0 6,18 * * 1-5": at(2026, 11, 23, 6, 0),
            "30 9 * * 0": at(2026, 11, 29, 9, 30),
            "30 9 * * 7": at(2026, 11, 29, 9, 30),
            "0 0 1 * *": at(2026, 12, 1, 0, 0),
            "5/20 23 * * *": at(2026, 11, 22, 23, 5),
        }
        for schedule, expected in cases.items():
            with self.subTest(schedule=schedule):
                self.assertEqual(proposals.next_firing(schedule, "", sunday), expected)

    def test_a_day_and_a_weekday_together_fire_on_either(self):
        """Cron's rule, and Kubernetes': the 13th or a Friday, whichever comes first."""
        self.assertEqual(proposals.next_firing("0 6 13 * 5", "", at(2026, 11, 22, 10, 5)), at(2026, 11, 27, 6, 0))
        self.assertEqual(proposals.next_firing("0 6 13 * 5", "", at(2026, 12, 12, 0, 0)), at(2026, 12, 13, 6, 0))

    def test_a_schedule_that_cannot_be_read_falls_back_to_a_week_after_the_plan(self):
        planned = NOW - timedelta(minutes=30)
        unreadable = (
            ("", LONDON),
            ("30 9 * * SAT", LONDON),
            ("@weekly", LONDON),
            ("30 9 ? * 6", LONDON),
            ("61 9 * * 6", LONDON),
            ("30 9 * * 6-2", LONDON),
            ("*/0 9 * * 6", LONDON),
            ("30 9 * *", LONDON),
            ("0 0 30 2 *", ""),
            (SATURDAYS, "Mars/Olympus"),
        )
        for schedule, zone in unreadable:
            with self.subTest(schedule=schedule, zone=zone):
                self.assertEqual(proposals.next_run(planned, NOW, schedule, zone), planned + timedelta(days=7))


class ReviewDocumentTests(unittest.TestCase):
    """What the page is sent: every verdict that is waiting, and who can still change it."""

    def test_a_delete_due_by_the_next_run_is_going(self):
        doc = document([stored(1)])
        self.assertEqual([item["id"] for item in doc["going"]], [1])
        self.assertEqual(doc["next_run_at"], (NOW + timedelta(days=7)).isoformat())

    def test_a_run_started_by_hand_lists_only_what_the_scheduled_run_finds_due(self):
        """The fixed clock is a Friday noon: Saturday's run comes before a verdict given today is due."""
        doc = document([stored(1), stored(2, made=NOW)], schedule=SATURDAYS, zone=LONDON)
        self.assertEqual(doc["next_run_at"], "2026-09-12T08:30:00+00:00")
        self.assertEqual([item["id"] for item in doc["going"]], [1])
        self.assertEqual([item["id"] for item in doc["later"]], [2])

    def test_a_delete_proposed_this_run_is_due_by_the_next_one(self):
        doc = document([stored(1, made=NOW + timedelta(minutes=40))])
        self.assertEqual([item["id"] for item in doc["going"]], [1])

    def test_a_delete_not_due_by_then_is_listed_as_later(self):
        doc = document([stored(1, made=NOW + timedelta(days=3))])
        self.assertEqual(doc["going"], [])
        self.assertEqual([item["id"] for item in doc["later"]], [1])

    def test_going_is_largest_first_and_cut_at_the_cap(self):
        doc = document([stored(1, size_gb=5), stored(2, size_gb=40), stored(3, size_gb=20)], cap=2)
        self.assertEqual([item["id"] for item in doc["going"]], [2, 3])
        self.assertEqual([item["id"] for item in doc["later"]], [1])
        self.assertEqual(doc["cap"], 2)

    def test_a_persons_delete_is_listed_ahead_of_larger_ones(self):
        doc = document([stored(1, size_gb=90), stored(2, size_gb=1, by="person")], cap=1)
        self.assertEqual([item["id"] for item in doc["going"]], [2])
        self.assertEqual(doc["going"][0]["by"], "person")

    def test_a_delete_can_be_kept_and_a_keep_deleted_instead(self):
        doc = document([stored(1), stored(2, verdict="keep")])
        self.assertEqual(doc["going"][0]["actions"], ["keep"])
        self.assertEqual(doc["kept"][0]["actions"], ["keep", "delete"])

    def test_an_item_carries_what_a_person_needs_to_decide(self):
        item = document([stored(1, size_gb=12.5, origin="requested", requested_by="Sam")])["going"][0]
        self.assertEqual(
            {key: item[key] for key in ("title", "year", "size_gb", "imdb_score", "imdb_votes", "plays")},
            {"title": "Show 1", "year": 2019, "size_gb": 12.5, "imdb_score": 4.9, "imdb_votes": 812, "plays": 0},
        )
        self.assertEqual((item["origin"], item["requested_by"], item["reason"]), ("requested", "Sam", REASON))
        self.assertEqual(item["due_at"], (WEEK_AGO + timedelta(days=6)).isoformat())

    def test_long_text_is_cut_and_a_wrong_type_dropped(self):
        item = document([stored(1, overview="x" * 5000, imdb_votes="many", year=True)])["going"][0]
        self.assertEqual(len(item["overview"]), review.OVERVIEW_LIMIT)
        self.assertIsNone(item["imdb_votes"])
        self.assertIsNone(item["year"])

    def test_what_the_engine_could_not_assess_can_be_kept_but_not_deleted_from_the_page(self):
        plan = {
            "review": [
                {"series_id": 7, "title": "Lost", "size_gb": 3.0, "blockers": ["record has no usable added date"]}
            ]
        }
        item = document([], plan)["needs_you"][0]
        self.assertEqual((item["by"], item["actions"]), ("engine", ["keep"]))
        self.assertEqual(item["reason"], "record has no usable added date")

    def test_a_review_the_judge_asked_for_can_go_either_way(self):
        plan = {"review": [{"series_id": 7, "title": "Unsure", "blockers": [], "reasons": ["already queued"]}]}
        result = {"plan_run_id": "run-plan", "escalated": [{"series_id": 8, "title": "New", "reason": "split"}]}
        items = document([], plan, result)["needs_you"]
        self.assertEqual(sorted(item["id"] for item in items), [7, 8])
        self.assertTrue(all(item["actions"] == ["keep", "delete"] and item["by"] == "judge" for item in items))

    def test_a_title_is_never_listed_twice(self):
        """A proposal the plan turned away is still on record when execute did not run."""
        plan = {"review": [{"series_id": 1, "title": "Show 1", "blockers": ["play history ambiguous"]}]}
        doc = document([stored(1)], plan)
        self.assertEqual([item["id"] for item in doc["going"]], [1])
        self.assertEqual(doc["needs_you"], [])

    def test_this_runs_deletions_are_listed_and_a_rehearsal_is_marked(self):
        result = {"plan_run_id": "run-plan", "deleted": [{"series_id": 9, "title": "Gone", "simulated": True}]}
        deleted = document([], result=result, mode="dry-run")["deleted"]
        self.assertEqual((deleted[0]["id"], deleted[0]["simulated"]), (9, True))

    def test_a_result_left_by_an_earlier_run_is_not_reported(self):
        result = {"plan_run_id": "last-week", "deleted": [{"series_id": 9}], "escalated": [{"series_id": 8}]}
        doc = document([], result=result)
        self.assertEqual((doc["deleted"], doc["needs_you"]), ([], []))

    def test_with_no_current_plan_the_proposals_are_still_shown(self):
        """A blocked or missing run must not empty the page: what is on record can still fall due."""
        doc = review.build(
            "tv",
            "series_id",
            None,
            None,
            {1: stored(1)},
            NOW,
            {"cap": 30, "mode": "dry-run", "blocks": ["no plan was written for this run"]},
        )
        self.assertEqual([item["id"] for item in doc["going"]], [1])
        self.assertEqual(doc["blocks"], ["no plan was written for this run"])
        self.assertEqual(doc["mode"], "dry-run")

    def test_the_document_names_its_library_version_and_run(self):
        doc = document([])
        self.assertEqual((doc["version"], doc["library"], doc["run_id"]), (2, "tv", "run-plan"))
        self.assertEqual(doc["generated_at"], NOW.isoformat())


class _Response(io.BytesIO):
    """A urllib response double."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:  # pylint: disable=too-few-public-methods
    """Captures the request, and answers or fails as told."""

    def __init__(self, outcome=None):
        self.outcome = outcome
        self.request = None

    def open(self, request, timeout=None):  # pylint: disable=unused-argument
        """Record the request; raise or return the configured outcome."""
        self.request = request
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or _Response(b"ok")


class PublishTests(unittest.TestCase):
    """The document goes to the page signed with a key both ends already hold."""

    KEY = "0123456789abcdef0123456789abcdef"

    def test_it_is_posted_to_the_librarys_own_path(self):
        opener = _Opener()
        note = review.publish("http://page:8080/", "tv", self.KEY, document([stored(1)]), opener=opener)
        self.assertEqual(opener.request.full_url, "http://page:8080/publish/tv")
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertIn("HTTP 200", note)

    def test_the_signature_covers_the_library_and_the_body(self):
        opener = _Opener()
        review.publish("http://page:8080", "tv", self.KEY, document([stored(1)]), opener=opener)
        body = opener.request.data
        expected = hmac.new(self.KEY.encode(), b"tv\n" + body, hashlib.sha256).hexdigest()
        self.assertEqual(opener.request.get_header("X-curator-signature"), f"sha256={expected}")

    def test_the_key_itself_is_never_sent(self):
        opener = _Opener()
        review.publish("http://page:8080", "tv", self.KEY, document([stored(1)]), opener=opener)
        sent = opener.request.data.decode() + json.dumps(dict(opener.request.header_items()))
        self.assertNotIn(self.KEY, sent)

    def test_a_document_signed_for_one_library_does_not_verify_for_the_other(self):
        body = b"{}"
        self.assertNotEqual(review.sign(self.KEY, "film", body), review.sign(self.KEY, "tv", body))

    def test_an_empty_key_signs_nothing(self):
        with self.assertRaises(review.PublishError):
            review.publish("http://page:8080", "tv", "", document([]), opener=_Opener())

    def test_a_refusal_or_an_unreachable_page_raises(self):
        refused = urllib.error.HTTPError("http://page", 401, "no", None, io.BytesIO())  # type: ignore[arg-type]
        self.addCleanup(refused.close)
        for failure in (refused, urllib.error.URLError("connection refused"), TimeoutError("timed out")):
            with self.subTest(failure=type(failure).__name__), self.assertRaises(review.PublishError):
                review.publish("http://page:8080", "tv", self.KEY, document([]), opener=_Opener(failure))


class DigestTests(unittest.TestCase):
    """The push says how many titles go at the next run, which is the number a person acts on."""

    def render(self, page, mode="act", **kwargs):
        """The digest for an ordinary, unblocked week."""
        plan = {
            "run_id": "run-plan",
            "mode": mode,
            "generated_at": (NOW - timedelta(minutes=30)).isoformat(),
            "counts": {"candidate": 3},
            "candidates": [],
        }
        result = {
            "plan_run_id": "run-plan",
            "deleted": [],
            "proposals": {
                "grace_days": 6,
                "recorded": {"delete": 2, "keep": 5},
                "marked_by_person": 1,
                "lapsed": 3,
            },
        }
        return reporting.render(plan, result, None, NOW, page=page, **kwargs)

    def page(self, mode="act", **overrides) -> dict:
        """A review document with two going, one later and one kept."""
        records = [stored(1, size_gb=40), stored(2, size_gb=20), stored(3, size_gb=5), stored(4, verdict="keep")]
        return {**document(records, cap=2, mode=mode), **overrides}

    def test_the_title_says_how_many_go_and_when(self):
        self.assertEqual(self.render(self.page()).title, "TV cleanup: 2 go on 18 Sep")

    def test_a_rehearsal_says_would(self):
        digest = self.render(self.page(mode="dry-run"), mode="dry-run")
        self.assertEqual(digest.title, "TV cleanup: 2 would go on 18 Sep")

    def test_the_push_carries_the_same_number_and_what_waits_behind_it(self):
        push = self.render(self.page()).push
        self.assertIn("<b>2 go on 18 Sep</b> unless you keep them · 1 waiting · 1 being kept", push)

    def test_a_week_with_nothing_due_says_so(self):
        digest = self.render(document([stored(4, verdict="keep")]))
        self.assertEqual(digest.title, "TV cleanup: nothing due on 18 Sep")
        self.assertNotIn("unless you keep them", digest.push)

    def test_the_text_lists_what_is_going_with_the_reason(self):
        text = self.render(self.page()).text
        self.assertIn("## Review window", text)
        self.assertIn("2 go on 18 sep: 60 GB (at most 2 a run)", text)
        self.assertRegex(text, rf"  - Show 1 \(2019\) 40 GB, due \d+ \w+: {re.escape(REASON)}")
        self.assertIn("Waiting their turn or not yet due: 1", text)
        self.assertIn("Being kept for a year once their wait is over: 1", text)
        self.assertNotIn("tagged", text)

    def test_the_text_lists_every_proposal_however_many(self):
        """With no review page the text report is the only full list, so nothing is cut from it."""
        records = [stored(n, size_gb=100 - n) for n in range(1, 41)] + [stored(99, verdict="keep")]
        text = self.render(document(records, cap=35)).text
        for n in (1, 35, 36, 40, 99):
            self.assertIn(f"  - Show {n} (2019) ", text)

    def test_the_text_says_what_this_run_put_on_record(self):
        text = self.render(self.page(), unpublished="").text
        self.assertIn(
            "This run: 2 delete and 5 keep proposed (each waits 6 days), 1 marked by a person, 3 lapsed", text
        )
        self.assertIn("Review page: updated", text)

    def test_a_page_that_was_not_updated_is_a_problem_on_the_phone(self):
        digest = self.render(self.page(), unpublished="review page returned 401")
        self.assertEqual(digest.title, "TV cleanup: review page not updated: review page returned 401")
        self.assertIn("Review page: NOT UPDATED (review page returned 401)", digest.text)

    def test_with_no_review_page_the_window_is_reported_and_no_page_is_mentioned(self):
        digest = self.render(self.page())
        self.assertIn("2 go on 18 Sep", digest.title)
        self.assertIn("## Review window", digest.text)
        self.assertNotIn("Review page", digest.text)
        self.assertNotIn("review page", digest.push)

    def test_without_a_review_document_the_digest_reads_as_before(self):
        digest = self.render(None)
        self.assertEqual(digest.title, "TV cleanup: 0 to review")
        self.assertNotIn("Review window", digest.text)

    def test_with_no_plan_the_digest_still_lists_what_falls_due(self):
        """A planner outage does not empty the window: the proposals on record still go at the next run."""
        digest = reporting.render(None, None, None, NOW, page=self.page(), unpublished="review page returned 401")
        self.assertIn("no plan", digest.title)
        self.assertIn("2 go on 18 Sep", digest.push)
        self.assertIn("review page not updated: review page returned 401", digest.push)
        self.assertIn("## Review window", digest.text)

    def test_with_no_plan_and_no_page_an_unreadable_window_is_said(self):
        digest = reporting.render(None, None, None, NOW, unreported="the proposals on record could not be read")
        self.assertIn("review window not reported", digest.push)
        self.assertIn("Review window: NOT REPORTED", digest.text)

    def test_a_blocked_run_says_how_to_accept_its_figures(self):
        plan = {
            "run_id": "run-20260911T080000-abc123",
            "mode": "act",
            "generated_at": NOW.isoformat(),
            "counts": {},
            "blocks": ["keep-tagged series fell 2.8% (1700 -> 1652), over the 2.0% limit"],
        }
        text = reporting.render(plan, None, None, NOW).text
        self.assertIn("CLEANUP_ACCEPT_BASELINE=run-20260911T080000-abc123", text)


class CurrentPlanTests(unittest.TestCase):
    """The page is told why nothing is going when a run was blocked or never planned."""

    def test_a_missing_plan_is_a_reason(self):
        self.assertEqual(reporting.current_plan(None, NOW), (None, ["no plan was written for this run"]))

    def test_a_stale_plan_is_not_handed_on(self):
        plan = {"generated_at": (NOW - timedelta(days=7)).isoformat()}
        current, blocks = reporting.current_plan(plan, NOW)
        self.assertIsNone(current)
        self.assertIn("168h old", blocks[0])

    def test_a_blocked_plan_is_current_and_carries_its_blocks(self):
        plan = {"generated_at": NOW.isoformat(), "blocks": ["play history not usable: timeout"]}
        self.assertEqual(reporting.current_plan(plan, NOW), (plan, ["play history not usable: timeout"]))

    def test_an_ordinary_plan_has_no_reasons(self):
        plan = {"generated_at": NOW.isoformat(), "blocks": []}
        self.assertEqual(reporting.current_plan(plan, NOW), (plan, []))


if __name__ == "__main__":
    unittest.main()
