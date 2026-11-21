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

from curatortv import __main__ as cli
from curatortv import reporting, review
from curatortv.ledger import Ledger
from curatortv.model import Outcome
from curatortv.planner import assess, lapsed_proposals

from .fixtures import (
    FakeS3,
    FakeSonarr,
    FakeTautulli,
    feed_provenance,
    make_availability,
    make_show,
    make_viewing,
    play_row,
    propose,
    series_record,
)
from .test_planner import context, run
from .test_review_window import REASON, on_record, snapshot


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
            "SONARR_API_KEY": "0123456789abcdef",
        }
        ledger = Ledger(cli.FileStore(self.root / "ledger"), "run-earlier", dry_run=True)
        propose(ledger, {"series_id": 1, "title": "Show 1", "size_gb": 4.0})
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
            for name in ("CLEANUP_WEBHOOK_URL", "CLEANUP_MAX_DELETIONS", "CLEANUP_REVIEW_LINK"):
                if name not in environment:
                    os.environ.pop(name, None)
            return cli.cmd_report(args)

    def test_the_document_is_built_from_the_ledger_and_published(self):
        self.assertEqual(self.report(), 0)
        url, library, key, doc = self.published[0]
        self.assertEqual((url, library, key), ("http://page:8080", "tv", "0123456789abcdef"))
        self.assertEqual([item["id"] for item in doc["going"]], [1])
        self.assertEqual(doc["mode"], "dry-run")

    def test_it_is_published_even_though_execute_never_ran(self):
        """No result file: the proposals on record are still what can fall due."""
        self.report()
        self.assertEqual(len(self.published[0][3]["going"]), 1)
        self.assertIn("Review page: updated", (self.root / "report.md").read_text(encoding="utf-8"))

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


class _PlanSonarr(FakeSonarr):
    """FakeSonarr with the reads ``plan`` makes."""

    def status(self) -> dict:
        """The preflight answer."""
        return {"version": "test"}

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

    def episode_history(self):
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
        self.shows = {
            1: make_show(series_id=1, tvdb_id=70001, tmdb_id=1001),
            2: make_show(series_id=2, tvdb_id=70002, tmdb_id=1002),
            3: make_show(series_id=3, tvdb_id=70003, tmdb_id=1003, tags={"cleanup-keep"}),
        }
        for series_id in (1, 3):
            self.ledger.write_proposal(
                on_record(make_show(series_id=series_id, tvdb_id=70000 + series_id, tmdb_id=1000 + series_id))
            )
        self.seen: dict = {}
        sources = SimpleNamespace(
            sonarr=_PlanSonarr(
                {n: series_record(n, tvdb_id=70000 + n, tmdb_id=1000 + n, imdb_id=f"tt000000{n}") for n in self.shows},
                {1: "keep"},
            ),
            tautulli=_PlanTautulli(),
            seerr=None,
            ledger=self.ledger,
            run_id="run-plan",
        )

        def assess_all(_series, _labels, ctx, _evidence, _ledger):
            self.seen["proposals"] = ctx.proposals
            return [
                assess(show, feed_provenance(), make_availability(), make_viewing(), ctx)
                for show in self.shows.values()
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
        self.assertEqual([entry["series_id"] for entry in plan["proposed"]], [1])
        self.assertEqual([entry["series_id"] for entry in plan["candidates"]], [2])
        self.assertEqual(plan["counts"], {"proposed": 1, "candidate": 1, "protected": 1})

    def test_a_proposal_the_plan_no_longer_upholds_is_named_for_removal(self):
        lapsed = self.plan()["lapsed_proposals"]
        self.assertEqual(
            [(item["series_id"], item["why"]) for item in lapsed], [(3, "now protected: carries cleanup-keep")]
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


class WeekToWeekTests(unittest.TestCase):
    """The whole loop, with the real planner between two real runs of execute."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.sonarr = FakeSonarr({1: series_record(1)}, {1: "keep", 2: "cleanup-keep"})
        self.ledger = Ledger(FakeS3(), "run-execute", dry_run=False)
        sources = SimpleNamespace(
            sonarr=self.sonarr, tautulli=FakeTautulli(), seerr=None, ledger=self.ledger, run_id="run-execute"
        )
        for name, value in (("build_sources", lambda: sources), ("log", lambda _message: None)):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, show=None) -> dict:
        """What the planner makes of the library, given the proposals now on record."""
        standing = self.ledger.read_proposals()
        assessed = [run(show, ctx=context(proposals=standing))] if show is not False else []
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
        verdicts = [{"series_id": 1, "verdict": verdict, "reason": REASON}] if verdict else []
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
        self.assertEqual(self.sonarr.deleted, [])

        second = self.plan()
        self.assertEqual((len(second["candidates"]), len(second["proposed"])), (0, 1))
        self.assertEqual([item["series_id"] for item in self.execute(second)["deleted"]], [1])
        self.assertEqual(self.sonarr.deleted, [1])
        self.assertEqual(self.ledger.read_proposals(), {})

    def test_a_keep_is_recorded_by_one_run_and_tagged_by_the_next(self):
        self.execute(self.plan(), "keep")
        self.assertEqual(self.sonarr.tag_calls, [])
        result = self.execute(self.plan())
        self.assertEqual(self.sonarr.tag_calls, [([1], 1)])
        self.assertEqual(result["judge_keeps_tagged"]["applied"], 1)
        self.assertEqual(self.ledger.read_proposals(), {})
        self.assertIn(1, self.ledger.read_decisions())

    def test_a_due_keep_that_a_persons_tag_forbids_is_closed_as_a_spare(self):
        """cleanup-dismissed keeps keep off; the proposal closes rather than waiting for a tag that never comes."""
        plan = self.plan()
        self.execute(plan, "keep")
        self.sonarr.tags_by_id[3] = "cleanup-dismissed"
        self.sonarr.series_by_id[1]["tags"] = [3]
        result = self.execute(self.plan())
        self.assertEqual(self.sonarr.tag_calls, [])
        self.assertEqual(result["judge_keeps_tagged"]["settled_untagged"], 1)
        self.assertEqual(self.ledger.read_proposals(), {})
        self.assertEqual(self.ledger.read_decisions()[1]["verdict"], "spared")

    def test_keeping_it_in_between_stops_the_delete_and_clears_the_record(self):
        self.execute(self.plan(), "delete")
        kept = make_show(tags={"kometa", "cleanup-keep"})
        self.sonarr.series_by_id[1]["tags"] = [2]
        second = self.plan(kept)
        self.assertEqual(second["proposed"], [])
        self.assertEqual(second["lapsed_proposals"][0]["why"], "now protected: carries cleanup-keep")
        self.assertEqual(self.execute(second)["deleted"], [])
        self.assertEqual(self.sonarr.deleted, [])
        self.assertEqual(self.ledger.read_proposals(), {})

    def test_somebody_watching_it_in_between_sends_it_back_to_the_judge(self):
        self.execute(self.plan(), "delete")
        standing = self.ledger.read_proposals()
        watched = run(viewing=make_viewing(viewers=1), ctx=context(proposals=standing))
        self.assertIs(watched.outcome, Outcome.CANDIDATE)
        self.assertEqual(lapsed_proposals(standing, [watched])[0]["series_id"], 1)

    def test_a_series_that_left_the_library_has_its_proposal_cleared(self):
        self.execute(self.plan(), "delete")
        del self.sonarr.series_by_id[1]
        gone = self.plan(False)
        self.assertEqual(gone["lapsed_proposals"][0]["why"], "no longer in the library")
        self.execute(gone)
        self.assertEqual(self.ledger.read_proposals(), {})


if __name__ == "__main__":
    unittest.main()
