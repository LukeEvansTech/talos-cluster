"""The review window through the commands: ``plan``, ``execute`` and ``report`` end to end.

``test_review_window`` covers the rules one at a time. These run the commands
themselves, with the real planner between two runs of ``execute``, so the
pieces are shown to fit: what one run records is what the next plan upholds and
the next run acts on.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from curator import __main__ as cli
from curator import reporting, review
from curator.acting import to_film
from curator.ledger import Ledger
from curator.model import Outcome
from curator.planner import assess, lapsed_proposals

from .fixtures import (
    FakeRadarr,
    FakeS3,
    FakeTautulli,
    feed_provenance,
    make_availability,
    make_film,
    make_viewing,
    play_row,
    propose,
)
from .test_executor import movie_record
from .test_planner import context, run
from .test_review_window import REASON, on_record, snapshot

# Somebody played it since the verdict: a fact a stored decision rests on.
WATCHED = make_viewing(completers=1)


class ReportCommandTests(unittest.TestCase):
    """``report`` publishes from the ledger when a page is configured, and is unchanged when none is."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        now = datetime.now(timezone.utc)
        plan = {"run_id": "run-plan", "mode": "dry-run", "generated_at": now.isoformat(), "counts": {}}
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        self.environment = {
            "LEDGER_DIR": str(self.root / "ledger"),
            "CLEANUP_MODE": "dry-run",
            "CLEANUP_REVIEW_URL": "http://page:8080",
            "RADARR_API_KEY": "0123456789abcdef",
        }
        ledger = Ledger(cli.FileStore(self.root / "ledger"), "run-earlier", dry_run=True)
        propose(ledger, {"movie_id": 1, "title": "Film 1", "size_gb": 4.0})
        self.published: list[tuple] = []
        self.posted: list[dict] = []
        for name, value in (("log", lambda _message: None),):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def report(self, publish=None, **environment) -> int:
        """Run the report command with the page's publish call replaced."""

        def record(url, library, key, doc):
            self.published.append((url, library, key, doc))
            return "review page updated, HTTP 200"

        def deliver(url, token, digest, link=""):
            self.posted.append({"url": url, "token": token, "digest": digest, "link": link})
            return "digest delivered, HTTP 200"

        args = argparse.Namespace(
            plan=str(self.root / "plan.json"),
            result=str(self.root / "missing.json"),
            judgement="",
            out=str(self.root / "report.md"),
        )
        with (
            mock.patch.dict(os.environ, {**self.environment, **environment}),
            mock.patch.object(review, "publish", publish or record),
            mock.patch.object(reporting, "post", deliver),
            mock.patch("builtins.print"),
        ):
            for name in ("CLEANUP_WEBHOOK_URL", "CLEANUP_MAX_DELETIONS", "CLEANUP_REVIEW_LINK", "CLEANUP_SCHEDULE"):
                if name not in environment:
                    os.environ.pop(name, None)
            return cli.cmd_report(args)

    def test_the_document_is_built_from_the_ledger_and_published(self):
        self.assertEqual(self.report(), 0)
        url, library, key, doc = self.published[0]
        self.assertEqual((url, library, key), ("http://page:8080", "film", "0123456789abcdef"))
        self.assertEqual([item["id"] for item in doc["going"]], [1])
        self.assertEqual(doc["mode"], "dry-run")

    def test_it_is_published_even_though_execute_never_ran(self):
        """No result file: the proposals on record are still what can fall due."""
        self.report()
        self.assertEqual(len(self.published[0][3]["going"]), 1)
        self.assertIn("Review page: updated", (self.root / "report.md").read_text(encoding="utf-8"))

    def test_the_next_run_is_dated_from_the_cronjobs_schedule(self):
        self.report(CLEANUP_SCHEDULE="30 9 * * 6", TZ="Europe/London")
        fires = datetime.fromisoformat(self.published[0][3]["next_run_at"])
        local = fires.astimezone(ZoneInfo("Europe/London"))
        self.assertEqual((local.isoweekday(), local.hour, local.minute), (6, 9, 30))
        self.assertLess(fires - datetime.now(timezone.utc), timedelta(days=7, minutes=1))

    def test_a_schedule_that_cannot_be_read_does_not_stop_the_document(self):
        self.assertEqual(self.report(CLEANUP_SCHEDULE="30 9 * * SAT"), 0)
        plan = json.loads((self.root / "plan.json").read_text(encoding="utf-8"))
        fires = datetime.fromisoformat(self.published[0][3]["next_run_at"])
        self.assertEqual(fires, datetime.fromisoformat(plan["generated_at"]) + timedelta(days=7))

    def test_a_page_that_refuses_fails_the_run_and_says_so(self):
        def refuse(*_args):
            raise review.PublishError("review page returned 401")

        self.assertEqual(self.report(publish=refuse), 11)
        self.assertIn("NOT UPDATED (review page returned 401)", (self.root / "report.md").read_text(encoding="utf-8"))

    def test_with_no_page_configured_nothing_is_published_and_nothing_fails(self):
        self.assertEqual(self.report(CLEANUP_REVIEW_URL=""), 0)
        self.assertEqual(self.published, [])

    def test_with_no_page_configured_the_digest_counts_the_window_and_names_no_page(self):
        self.report(CLEANUP_REVIEW_URL="")
        text = (self.root / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Review window", text)
        self.assertNotIn("Review page", text)

    def test_with_no_page_configured_an_unreadable_window_is_still_a_failure(self):
        """The proposals that fall due could not be read: said on the phone, and the Job fails."""
        self.assertEqual(self.report(CLEANUP_REVIEW_URL="", CLEANUP_MAX_DELETIONS="lots"), 12)
        text = (self.root / "report.md").read_text(encoding="utf-8")
        self.assertIn("Review window: NOT REPORTED (the proposals on record could not be read", text)
        self.assertNotIn("Review page", text)

    def test_the_published_document_carries_the_digest_as_it_went_out(self):
        self.report(CLEANUP_WEBHOOK_URL="http://hooks/cleanup")
        sent = self.posted[0]["digest"]
        summary = self.published[0][3]["summary"]
        self.assertEqual(self.published[0][3]["version"], 2)
        self.assertEqual(summary["digest"], {"title": sent.title, "message": sent.push, "text": sent.text})
        self.assertEqual(summary["digest"]["text"], (self.root / "report.md").read_text(encoding="utf-8"))

    def test_the_push_links_to_the_page_when_both_are_configured(self):
        self.report(CLEANUP_WEBHOOK_URL="http://hooks/cleanup", CLEANUP_REVIEW_LINK="https://review.example")
        self.assertEqual(self.posted[0]["link"], "https://review.example")

    def test_the_push_carries_no_link_without_a_page(self):
        self.report(
            CLEANUP_WEBHOOK_URL="http://hooks/cleanup",
            CLEANUP_REVIEW_URL="",
            CLEANUP_REVIEW_LINK="https://review.example",
        )
        self.assertEqual(self.posted[0]["link"], "")

    def test_the_push_carries_no_link_when_none_is_configured(self):
        self.report(CLEANUP_WEBHOOK_URL="http://hooks/cleanup")
        self.assertEqual(self.posted[0]["link"], "")

    def test_a_page_that_refuses_is_named_in_the_push_that_goes_out(self):
        def refuse(*_args):
            raise review.PublishError("review page returned 401")

        self.assertEqual(self.report(publish=refuse, CLEANUP_WEBHOOK_URL="http://hooks/cleanup"), 11)
        self.assertIn("review page not updated: review page returned 401", self.posted[0]["digest"].push)

    def test_a_rehearsal_does_not_publish_the_real_record(self):
        self.assertEqual(self.report(CLEANUP_MODE="act"), 0)
        self.assertEqual(self.published[0][3]["going"], [])
        self.assertEqual(self.published[0][3]["mode"], "act")

    def test_a_cap_that_cannot_be_read_does_not_stop_the_digest(self):
        self.assertEqual(self.report(CLEANUP_MAX_DELETIONS="lots"), 11)
        self.assertEqual(self.published, [])
        self.assertIn("could not be read", (self.root / "report.md").read_text(encoding="utf-8"))

    def test_the_cap_in_force_is_the_one_published(self):
        self.report(CLEANUP_MAX_DELETIONS="7")
        self.assertEqual(self.published[0][3]["cap"], 7)


class _PlanRadarr(FakeRadarr):
    """FakeRadarr with the reads ``plan`` makes."""

    def status(self) -> dict:
        """The preflight answer."""
        return {"version": "test"}

    def collections(self) -> list[dict]:
        """One unmonitored collection: with none at all the engine reads the data as not loaded."""
        return [{"title": "Elsewhere", "monitored": False, "movies": []}]

    def exclusions(self) -> list[dict]:
        """No import exclusions."""
        return []

    def history_horizon(self) -> str:
        """How far back the import history reaches."""
        return "2020-01-01T00:00:00Z"

    def import_events(self) -> dict:
        """No import events; assessment is replaced in these tests."""
        return {}


class _PlanTautulli:
    """A history service with one play on record, which is what makes the history believable."""

    def preflight(self) -> str:
        """The liveness answer."""
        return "ok"

    def movie_history(self):
        """One completed play of something else."""
        return [play_row(900, 100, 95, 1)], {"declared": 1, "retrieved": 1, "complete": True}

    def metadata(self, _key: int):
        """Nothing resolves; the play is unattributed."""
        return None


class PlanCommandTests(unittest.TestCase):
    """``plan`` reads the proposals on record, lists what stands and what lapsed, and honours an accepted baseline."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.store = FakeS3()
        self.ledger = Ledger(self.store, "run-plan", dry_run=True)
        self.films = {
            1: make_film(movie_id=1, tmdb_id=1001),
            2: make_film(movie_id=2, tmdb_id=1002),
            3: make_film(movie_id=3, tmdb_id=1003, tags={"cleanup-keep"}),
        }
        for movie_id in (1, 3):
            self.ledger.write_proposal(on_record(make_film(movie_id=movie_id, tmdb_id=1000 + movie_id)))
        self.seen: dict = {}
        sources = SimpleNamespace(
            radarr=_PlanRadarr({n: movie_record(n, tmdb_id=1000 + n) for n in self.films}, {1: "keep"}),
            tautulli=_PlanTautulli(),
            seerr=None,
            ledger=self.ledger,
            run_id="run-plan",
        )

        def assess_all(_movies, _labels, ctx, _evidence, _ledger):
            self.seen["proposals"] = ctx.proposals
            return [
                assess(film, feed_provenance(), make_availability(), make_viewing(), ctx)
                for film in self.films.values()
            ]

        for name, value in (
            ("build_sources", lambda: sources),
            ("log", lambda _message: None),
            ("assess_library", assess_all),
        ):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, **settings: str) -> dict:
        """Run the plan command and return the plan it wrote."""
        with mock.patch.dict(os.environ, settings):
            for name in ("CLEANUP_ACCEPT_BASELINE", "CLEANUP_SCOPE_START", "CLEANUP_JUDGE_LIMIT"):
                if name not in settings:
                    os.environ.pop(name, None)
            self.assertEqual(cli.cmd_plan(argparse.Namespace(out=str(self.root))), 0)
        return json.loads((self.root / "plan.json").read_text(encoding="utf-8"))

    def test_assessment_is_given_the_proposals_on_record(self):
        self.plan()
        self.assertEqual(sorted(self.seen["proposals"]), [1, 3])

    def test_a_standing_proposal_is_listed_and_not_offered_to_the_judge(self):
        plan = self.plan()
        self.assertEqual([entry["movie_id"] for entry in plan["proposed"]], [1])
        self.assertEqual([entry["movie_id"] for entry in plan["candidates"]], [2])
        self.assertEqual(plan["counts"], {"proposed": 1, "candidate": 1, "protected": 1})

    def test_a_proposal_the_plan_no_longer_upholds_is_named_for_removal(self):
        lapsed = self.plan()["lapsed_proposals"]
        self.assertEqual(
            [(item["movie_id"], item["why"]) for item in lapsed], [(3, "now protected: carries cleanup-keep")]
        )

    def test_the_plan_changes_no_proposal_itself(self):
        """Only execute edits the record, and it refuses a blocked plan; a plan that edited it would not."""
        self.plan()
        self.assertEqual(sorted(self.ledger.read_proposals()), [1, 3])

    def test_the_stored_plan_keeps_its_snapshot_and_not_the_proposed_list(self):
        self.plan()
        stored_plan = self.ledger.read_plan("run-plan")
        self.assertNotIn("proposed", stored_plan)
        self.assertEqual(stored_plan["snapshot"]["library_size"], 3)

    def test_a_fall_blocks_the_run_until_its_figures_are_accepted(self):
        earlier = Ledger(self.store, "run-blocked", dry_run=True)
        earlier.write_baseline(
            snapshot(datetime.now(timezone.utc) - timedelta(days=14), library_size=3, keep_tag_count=100)
        )
        blocked = self.plan()
        self.assertTrue(any("keep-tagged" in block for block in blocked["blocks"]))
        earlier.record_plan({"run_id": "run-blocked", "snapshot": blocked["snapshot"]})

        again = self.plan()
        self.assertTrue(again["blocks"], "an unaccepted fall keeps blocking")

        accepted = self.plan(CLEANUP_ACCEPT_BASELINE="run-blocked")
        self.assertEqual(accepted["blocks"], [])
        self.assertEqual(self.ledger.read_baseline()["keep_tag_count"], 0)


class _ExecuteRadarr(FakeRadarr):  # pylint: disable=too-few-public-methods
    """FakeRadarr with the collection listing execute reads."""

    def collections(self) -> list[dict]:
        """No collections, so none is monitored."""
        return []


class WeekToWeekTests(unittest.TestCase):
    """The whole loop, with the real planner between two real runs of execute."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.radarr = _ExecuteRadarr({1: movie_record(1)}, {1: "keep", 2: "cleanup-keep"})
        self.ledger = Ledger(FakeS3(), "run-execute", dry_run=False)
        sources = SimpleNamespace(
            radarr=self.radarr, tautulli=FakeTautulli(), seerr=None, ledger=self.ledger, run_id="run-execute"
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, film=None, now=None, viewing=None) -> dict:
        """What the planner makes of the library, given the proposals and decisions now on record."""
        standing = self.ledger.read_proposals()
        clock = {"now": now} if now else {}
        ctx = context(proposals=standing, decisions=self.ledger.read_decisions(), **clock)
        assessed = [run(film, viewing=viewing, ctx=ctx)] if film is not False else []
        return {
            "run_id": "plan-1",
            "mode": "act",
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [a.to_json() for a in assessed if a.outcome is Outcome.CANDIDATE],
            "proposed": [a.to_json() for a in assessed if a.outcome is Outcome.PROPOSED],
            "lapsed_proposals": lapsed_proposals(standing, assessed),
        }

    def execute(self, plan: dict, verdict: str = "") -> dict:
        """One act-mode run against this plan, with the judge's verdict on its candidate if it has one."""
        verdicts = [{"movie_id": 1, "verdict": verdict, "reason": REASON}] if verdict else []
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.root / "recs.json").write_text(
            json.dumps({"plan_run_id": "plan-1", "recommendations": verdicts}), encoding="utf-8"
        )
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"),
            recommendations=str(self.root / "recs.json"),
            out=str(self.root / "result.json"),
            read_only=False,
        )
        # No wait, so the second run of a test finds the first run's verdict due.
        with mock.patch.dict(os.environ, {"CLEANUP_GRACE_DAYS": "0", "TAG_KEEPS_IN_DRY_RUN": "false"}):
            os.environ.pop("CLEANUP_MAX_DELETIONS", None)
            self.assertEqual(cli.cmd_execute(args), 0)
        return json.loads((self.root / "result.json").read_text(encoding="utf-8"))

    def test_a_delete_is_recorded_by_one_run_and_carried_out_by_the_next(self):
        first = self.plan()
        self.assertEqual(len(first["candidates"]), 1)
        self.assertEqual(self.execute(first, "delete")["deleted"], [])
        self.assertEqual(self.radarr.deleted, [])

        second = self.plan()
        self.assertEqual((len(second["candidates"]), len(second["proposed"])), (0, 1))
        self.assertEqual([item["movie_id"] for item in self.execute(second)["deleted"]], [1])
        self.assertEqual(self.radarr.deleted, [1])
        self.assertEqual(self.ledger.read_proposals(), {})

    def live(self):
        """The film as the planner would read it from the live record, so fingerprints match across runs."""
        return to_film(self.radarr.movies_by_id[1], self.radarr.tags_by_id)

    def kept_for_a_year(self) -> dict:
        """Two runs: the judge keeps the film, and the next run finds the keep due."""
        self.execute(self.plan(self.live()), "keep")
        return self.execute(self.plan(self.live()))

    def test_a_due_keep_becomes_a_one_year_decision_with_no_tag(self):
        result = self.kept_for_a_year()
        self.assertEqual(self.radarr.tag_calls, [], "the judge never writes keep")
        self.assertEqual(result["judge_keeps"]["kept"], 1)
        self.assertEqual(self.ledger.read_proposals(), {})
        decision = self.ledger.read_decisions()[1]
        self.assertEqual(decision["verdict"], "spared")
        until = datetime.fromisoformat(decision["kept_until"])
        self.assertLess(abs((until - datetime.now(timezone.utc) - timedelta(days=365)).total_seconds()), 120)

    def test_a_kept_film_is_spared_and_not_offered_to_the_judge(self):
        self.kept_for_a_year()
        third = self.plan(self.live())
        self.assertEqual((third["candidates"], third["proposed"]), ([], []))
        spared = run(self.live(), ctx=context(decisions=self.ledger.read_decisions()))
        self.assertIs(spared.outcome, Outcome.SPARED)
        self.assertIn("kept by the judge on", spared.reasons[0])
        self.assertIn("until", spared.reasons[0])

    def test_a_kept_film_is_a_candidate_again_once_its_year_is_up(self):
        self.kept_for_a_year()
        later = self.plan(self.live(), now=datetime.now(timezone.utc) + timedelta(days=366))
        self.assertEqual([entry["movie_id"] for entry in later["candidates"]], [1])

    def test_a_changed_fact_reopens_a_kept_film_early(self):
        self.kept_for_a_year()
        watched = self.plan(self.live(), viewing=WATCHED)
        self.assertEqual([entry["movie_id"] for entry in watched["candidates"]], [1])

    def test_cleanup_keep_added_in_the_review_week_wins(self):
        """A person's keep is for good: the proposal lapses and no one-year decision is written."""
        self.execute(self.plan(self.live()), "keep")
        self.radarr.movies_by_id[1]["tags"] = [2]
        second = self.plan(self.live())
        self.assertEqual(second["proposed"], [])
        self.assertEqual(second["lapsed_proposals"][0]["why"], "now protected: carries cleanup-keep")
        result = self.execute(second)
        self.assertEqual(result["judge_keeps"]["due"], 0)
        self.assertEqual(self.ledger.read_proposals(), {})
        self.assertNotIn(1, self.ledger.read_decisions())
        self.assertEqual(self.radarr.tag_calls, [])

    def test_a_due_keep_on_a_dismissed_film_is_still_kept_for_a_year(self):
        """cleanup-dismissed is the person's spare; the judge's year is recorded beside it, and no tag is written."""
        self.execute(self.plan(self.live()), "keep")
        due = self.plan(self.live())
        # Dismissed after the plan was written and before execute re-read the record.
        self.radarr.tags_by_id[3] = "cleanup-dismissed"
        self.radarr.movies_by_id[1]["tags"] = [3]
        result = self.execute(due)
        self.assertEqual(self.radarr.tag_calls, [])
        self.assertEqual(result["judge_keeps"]["kept"], 1)
        self.assertEqual(self.ledger.read_proposals(), {})
        self.assertEqual(self.ledger.read_decisions()[1]["verdict"], "spared")

    def test_keeping_it_in_between_stops_the_delete_and_clears_the_record(self):
        self.execute(self.plan(), "delete")
        kept = make_film(tags={"src-tmdb-popular", "cleanup-keep"})
        self.radarr.movies_by_id[1]["tags"] = [2]
        second = self.plan(kept)
        self.assertEqual(second["proposed"], [])
        self.assertEqual(second["lapsed_proposals"][0]["why"], "now protected: carries cleanup-keep")
        self.assertEqual(self.execute(second)["deleted"], [])
        self.assertEqual(self.radarr.deleted, [])
        self.assertEqual(self.ledger.read_proposals(), {})

    def test_somebody_watching_it_in_between_sends_it_back_to_the_judge(self):
        self.execute(self.plan(), "delete")
        standing = self.ledger.read_proposals()
        watched = run(viewing=make_viewing(completers=1), ctx=context(proposals=standing))
        self.assertIs(watched.outcome, Outcome.CANDIDATE)
        self.assertEqual(lapsed_proposals(standing, [watched])[0]["movie_id"], 1)

    def test_a_film_that_left_the_library_has_its_proposal_cleared(self):
        self.execute(self.plan(), "delete")
        del self.radarr.movies_by_id[1]
        gone = self.plan(False)
        self.assertEqual(gone["lapsed_proposals"][0]["why"], "no longer in the library")
        self.execute(gone)
        self.assertEqual(self.ledger.read_proposals(), {})


if __name__ == "__main__":
    unittest.main()
