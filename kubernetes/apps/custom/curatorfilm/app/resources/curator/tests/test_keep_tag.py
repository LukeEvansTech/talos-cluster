"""Allow-list maintenance: the rating bar's tags, and the keeps the judge hands down.

Neither may overturn a person's decision, a dry run writes nothing unless
``TAG_KEEPS_IN_DRY_RUN`` says so, and a tag that did not land is never recorded
as a settled keep.
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

from curator import __main__ as cli
from curator import reporting
from curator.__main__ import apply_keep_tags
from curator.ledger import Ledger
from curator.planner import keep_tag_targets

from .fixtures import FakeRadarr, FakeS3, FakeTautulli, make_film, propose
from .test_executor import movie_record


class KeepTagTests(unittest.TestCase):
    """Add-only, and never on top of somebody's decision."""

    def test_film_over_the_bar_is_tagged(self):
        film = make_film(tags=frozenset(), imdb_votes=6000, imdb_score=7.0)
        self.assertEqual(keep_tag_targets([film]), [film])

    def test_thin_votes_do_not_qualify(self):
        film = make_film(tags=frozenset(), imdb_votes=1200, imdb_score=8.1)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_low_score_does_not_qualify(self):
        film = make_film(tags=frozenset(), imdb_votes=90000, imdb_score=6.4)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_missing_rating_does_not_qualify(self):
        film = make_film(tags=frozenset(), imdb_votes=None, imdb_score=None)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_already_tagged_is_not_retagged(self):
        film = make_film(tags={"keep"}, imdb_votes=6000, imdb_score=7.0)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_keep_review_item_is_never_promoted(self):
        """The queue exists so a person decides; tagging it decides for them."""
        film = make_film(tags={"keep-review"}, imdb_votes=200000, imdb_score=8.4)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_human_eligibility_is_not_silently_reversed(self):
        film = make_film(tags={"cleanup-eligible"}, imdb_votes=200000, imdb_score=8.4)
        self.assertEqual(keep_tag_targets([film]), [])

    def test_human_dismissal_is_not_silently_reversed(self):
        film = make_film(tags={"cleanup-dismissed"}, imdb_votes=200000, imdb_score=8.4)
        self.assertEqual(keep_tag_targets([film]), [])


LABELS = {1: "keep", 5: "cleanup-eligible", 6: "cleanup-dismissed", 7: "keep-review", 8: "src-tmdb-popular"}


class JudgeKeepTagTests(unittest.TestCase):
    """A keep is written to Radarr only where a person has not decided otherwise."""

    def radarr(self, tags=None) -> FakeRadarr:
        """One film, with the given live tag ids."""
        return FakeRadarr({1: movie_record(tags=tags or [])}, dict(LABELS))

    def apply(self, radarr, dry_run=False, flag=False):
        """Apply a keep to film 1, re-checked against its live record."""
        return apply_keep_tags(
            radarr,
            [{"movie_id": 1}],
            dry_run=dry_run,
            live={m["id"]: m for m in radarr.movies()},
            tag_labels=LABELS,
            tag_in_dry_run=flag,
        )

    def test_act_mode_applies_and_confirms_the_tag(self):
        radarr = self.radarr()
        outcome = self.apply(radarr)
        self.assertEqual((outcome["applied"], outcome["unconfirmed"]), (1, []))
        self.assertEqual(radarr.tag_calls, [([1], 1)])
        self.assertIn(1, radarr.movies_by_id[1]["tags"])

    def test_a_dry_run_applies_nothing_without_the_flag(self):
        radarr = self.radarr()
        outcome = self.apply(radarr, dry_run=True)
        self.assertEqual(outcome["applied"], 0)
        self.assertEqual(radarr.tag_calls, [])

    def test_a_dry_run_applies_the_tag_when_the_flag_is_set(self):
        radarr = self.radarr()
        outcome = self.apply(radarr, dry_run=True, flag=True)
        self.assertEqual(outcome["applied"], 1)
        self.assertIn("TAG_KEEPS_IN_DRY_RUN", outcome["note"])

    def test_a_persons_decision_tag_withdraws_the_keep(self):
        for tag in (5, 6, 7):
            with self.subTest(tag=LABELS[tag]):
                radarr = self.radarr(tags=[tag])
                self.assertEqual(self.apply(radarr)["withdrawn"], 1)
                self.assertEqual(radarr.tag_calls, [])

    def test_a_missing_keep_tag_is_created_first(self):
        radarr = FakeRadarr({1: movie_record()}, {8: "src-tmdb-popular"})
        outcome = apply_keep_tags(radarr, [{"movie_id": 1}], dry_run=False)
        self.assertEqual(outcome["applied"], 1)
        self.assertIn("keep", radarr.tags_by_id.values())

    def test_a_tag_that_did_not_land_is_reported_unconfirmed(self):
        """The editor endpoint answers 202 for a tag it never applies."""
        radarr = self.radarr()
        radarr.tags_that_do_not_land = {1}
        outcome = self.apply(radarr)
        self.assertEqual((outcome["applied"], outcome["unconfirmed"]), (0, [1]))


class _Radarr(FakeRadarr):  # pylint: disable=too-few-public-methods
    """FakeRadarr with the collection listing execute reads."""

    def collections(self) -> list[dict]:
        """No collections, so none is monitored."""
        return []


class JudgeKeepsThroughExecute(unittest.TestCase):
    """End to end through ``cmd_execute``: a keep joins the allow list once its wait is over."""

    CANDIDATE = {"origin": "unknown", "availability_source": "radarr-history", "history_status": "ok"}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        movies = {
            1: movie_record(1, tags=[8]),
            2: movie_record(2, tags=[8], tmdb_id=2000),
            3: movie_record(3, tags=[5], tmdb_id=3000),
            4: movie_record(4, tags=[8], tmdb_id=4000),
        }
        self.radarr = _Radarr(movies, dict(LABELS))
        self.store = FakeS3()

    def execute(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self, mode: str = "dry-run", flag: str = "false", age_days: float = 7.0, judged: str = ""
    ) -> tuple[int, dict]:
        """Run execute in ``mode`` and return its exit code and result file.

        Three verdicts from an earlier run are on record: delete film 1, keep 2
        and keep 3 (which a person has since marked cleanup-eligible). ``judged``
        is this run's verdict on film 4, when it gives one.
        """
        dry_run = mode == "dry-run"
        ledger = Ledger(self.store, "run-execute", dry_run=dry_run)
        plan = {
            "run_id": "plan-1",
            "mode": mode,
            "generated_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "blocks": [],
            "candidates": [{"movie_id": 4, **self.CANDIDATE}] if judged else [],
            "proposed": [
                propose(ledger, {"movie_id": 1, **self.CANDIDATE}, "delete", age_days=age_days),
                propose(ledger, {"movie_id": 2, **self.CANDIDATE}, "keep", age_days=age_days),
                propose(ledger, {"movie_id": 3, **self.CANDIDATE}, "keep", age_days=age_days),
            ],
            "keep_tag_additions": [],
        }
        verdicts = [{"movie_id": 4, "verdict": judged, "reason": "cult standing, still revived"}] if judged else []
        (self.root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        (self.root / "recs.json").write_text(
            json.dumps({"plan_run_id": "plan-1", "recommendations": verdicts}), encoding="utf-8"
        )
        sources = SimpleNamespace(
            radarr=self.radarr,
            tautulli=FakeTautulli(),
            seerr=None,
            ledger=ledger,
            run_id="run-execute",
        )
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"),
            recommendations=str(self.root / "recs.json"),
            out=str(self.root / "result.json"),
            read_only=dry_run,
        )
        with (
            mock.patch.object(cli, "build_sources", lambda: sources),
            mock.patch.object(cli, "log", lambda _message: None),
            mock.patch.dict(os.environ, {"TAG_KEEPS_IN_DRY_RUN": flag}),
        ):
            code = cli.cmd_execute(args)
        return code, json.loads((self.root / "result.json").read_text(encoding="utf-8"))

    def decisions(self, prefix: str = "") -> list[str]:
        """The spare decisions on record."""
        return self.store.list_keys(f"{prefix}decisions/")

    def proposed(self, prefix: str = "") -> list[str]:
        """The proposals still on record."""
        return self.store.list_keys(f"{prefix}proposals/")

    def test_an_act_run_tags_the_due_keep_and_deletes_the_due_delete(self):
        code, result = self.execute("act")
        self.assertEqual(code, 0)
        self.assertEqual(self.radarr.deleted, [1])
        self.assertEqual(self.radarr.tag_calls, [([2], 1)])
        self.assertEqual(result["judge_keeps_tagged"]["applied"], 1)
        # Film 3 carries cleanup-eligible: a person's decision outranks the judge.
        self.assertEqual(result["judge_keeps_tagged"]["withdrawn"], 1)

    def test_what_was_carried_out_leaves_the_record_and_what_was_not_stays(self):
        self.execute("act")
        self.assertEqual(self.proposed(), ["proposals/3.json"])

    def test_a_dry_run_writes_no_tag_and_deletes_nothing(self):
        code, result = self.execute("dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.radarr.tag_calls, [])
        self.assertEqual(self.radarr.deleted, [])
        self.assertEqual(result["judge_keeps_tagged"]["applied"], 0)
        self.assertTrue(all(d.get("simulated") for d in result["deleted"]))
        self.assertEqual(len(self.proposed("dry-run/")), 3)

    def test_a_dry_run_tags_due_keeps_only_when_the_flag_allows_it(self):
        code, result = self.execute("dry-run", flag="true")
        self.assertEqual(code, 0)
        self.assertEqual(self.radarr.tag_calls, [([2], 1)])
        self.assertEqual(self.radarr.deleted, [])
        self.assertEqual(result["judge_keeps_tagged"]["applied"], 1)
        self.assertNotIn("dry-run/proposals/2.json", self.proposed("dry-run/"))

    def test_a_keep_inside_its_wait_is_not_tagged_in_any_mode(self):
        """The window is the point: a keep is permanent, so a person gets the same week to refuse it."""
        for mode, flag in (("act", "false"), ("dry-run", "true")):
            with self.subTest(mode=mode):
                _, result = self.execute(mode, flag=flag, age_days=1)
                self.assertEqual(self.radarr.tag_calls, [])
                self.assertEqual(self.radarr.deleted, [])
                self.assertEqual(result["judge_keeps_tagged"]["requested"], 0)

    def test_a_keep_given_this_run_is_put_on_record_and_not_tagged(self):
        for mode, flag in (("act", "false"), ("dry-run", "true")):
            with self.subTest(mode=mode):
                _, result = self.execute(mode, flag=flag, age_days=1, judged="keep")
                self.assertEqual(self.radarr.tag_calls, [])
                self.assertEqual(result["proposals"]["recorded"], {"delete": 0, "keep": 1})
                self.assertIn(f"{'dry-run/' if mode == 'dry-run' else ''}proposals/4.json", self.store.list_keys(""))

    def test_an_unconfirmed_tag_fails_the_run_and_is_not_recorded_as_settled(self):
        """Recorded as a spare it would never be judged again, and never be tagged either."""
        self.radarr.tags_that_do_not_land = {2}
        code, result = self.execute("act")
        self.assertEqual(code, 8)
        self.assertEqual(result["judge_keeps_tagged"]["unconfirmed"], [2])
        self.assertNotIn("decisions/2.json", self.decisions())
        self.assertIn("proposals/2.json", self.proposed())

    def test_a_confirmed_keep_is_recorded(self):
        self.execute("act")
        self.assertIn("decisions/2.json", self.decisions())
        self.assertNotIn("proposals/2.json", self.proposed())

    def test_the_digest_reports_the_judges_keep_tags(self):
        _, result = self.execute("act")
        now = datetime.now(timezone.utc)
        plan = {"run_id": "plan-1", "generated_at": now.isoformat(), "counts": {}}
        text = reporting.render(plan, result, None, now).text
        self.assertIn("## Keep tag from the judge: 1 requested, 1 applied, 1 withdrawn", text)

    def test_the_digest_names_an_unconfirmed_keep_tag(self):
        self.radarr.tags_that_do_not_land = {2}
        _, result = self.execute("act")
        now = datetime.now(timezone.utc)
        plan = {"run_id": "plan-1", "generated_at": now.isoformat(), "counts": {}}
        self.assertIn("1 keep tag not confirmed", reporting.render(plan, result, None, now).push)


if __name__ == "__main__":
    unittest.main()
