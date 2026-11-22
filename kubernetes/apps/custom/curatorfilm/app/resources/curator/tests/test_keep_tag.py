"""Allow-list maintenance: the rating bar's tags, and the keeps the judge hands down.

Only the rating bar writes ``keep``: a dry run writes it only when
``TAG_KEEPS_IN_DRY_RUN`` says so, and a tag that did not land fails the run. A
judge's due keep writes no tag at all; it becomes a decision that spares the
film for ``CLEANUP_KEEP_DAYS``. Neither may overturn a person's decision.
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
    """End to end through ``cmd_execute``: a due keep becomes a one-year decision, never a tag."""

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
        self.radarr = _Radarr(movies, {**LABELS, 9: "cleanup-keep"})
        self.store = FakeS3()

    def execute(  # pylint: disable=too-many-arguments,too-many-positional-arguments
        self,
        mode: str = "dry-run",
        flag: str = "false",
        age_days: float = 7.0,
        judged: str = "",
        keep_days: str = "",
        additions: tuple[int, ...] = (),
    ) -> tuple[int, dict]:
        """Run execute in ``mode`` and return its exit code and result file.

        Three verdicts from an earlier run are on record: delete film 1, keep 2
        and keep 3 (which a person has since marked cleanup-eligible). ``judged``
        is this run's verdict on film 4, when it gives one; ``additions`` are the
        films the rating bar asks to tag.
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
            "keep_tag_additions": [{"movie_id": movie_id} for movie_id in additions],
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
            mock.patch.dict(os.environ, {"TAG_KEEPS_IN_DRY_RUN": flag, "CLEANUP_KEEP_DAYS": keep_days}),
        ):
            code = cli.cmd_execute(args)
        return code, json.loads((self.root / "result.json").read_text(encoding="utf-8"))

    def decisions(self, prefix: str = "") -> list[str]:
        """The spare decisions on record."""
        return self.store.list_keys(f"{prefix}decisions/")

    def decision(self, movie_id: int, prefix: str = "") -> dict:
        """One stored decision."""
        return json.loads(self.store.get(f"{prefix}decisions/{movie_id}.json"))

    def proposed(self, prefix: str = "") -> list[str]:
        """The proposals still on record."""
        return self.store.list_keys(f"{prefix}proposals/")

    def assert_kept_for(self, decision: dict, days: int) -> None:
        """The decision spares the film until ``days`` from now, and says so."""
        until = datetime.fromisoformat(decision["reconsider_after"])
        expected = datetime.now(timezone.utc) + timedelta(days=days)
        self.assertLess(abs((until - expected).total_seconds()), 120)
        self.assertEqual(decision["kept_until"], decision["reconsider_after"])
        self.assertEqual(decision["verdict"], "spared")

    def test_an_act_run_keeps_the_due_keep_for_a_year_and_deletes_the_due_delete(self):
        code, result = self.execute("act")
        self.assertEqual(code, 0)
        self.assertEqual(self.radarr.deleted, [1])
        self.assertEqual(self.radarr.tag_calls, [], "a judge's keep writes no tag")
        self.assertEqual((result["judge_keeps"]["due"], result["judge_keeps"]["kept"]), (2, 1))
        self.assert_kept_for(self.decision(2), 365)
        # Film 3 carries cleanup-eligible: a person's delete outranks the judge, and replaces it at the next plan.
        self.assertEqual(result["judge_keeps"]["left_for_person"], [3])
        self.assertNotIn("decisions/3.json", self.decisions())

    def test_what_was_carried_out_leaves_the_record_and_what_was_not_stays(self):
        self.execute("act")
        self.assertEqual(self.proposed(), ["proposals/3.json"])

    def test_keep_days_sets_how_long_the_keep_lasts(self):
        self.execute("act", keep_days="30")
        self.assert_kept_for(self.decision(2), 30)

    def test_keep_days_of_zero_is_refused_before_anything_is_written(self):
        with self.assertRaises(SystemExit):
            self.execute("act", keep_days="0")
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.radarr.deleted, [])

    def test_a_cleanup_keep_added_in_the_review_week_wins(self):
        """A person's keep is for good: the judge's year is not recorded over it."""
        self.radarr.movies_by_id[2]["tags"] = [8, 9]
        code, result = self.execute("act")
        self.assertEqual(code, 0)
        self.assertEqual(result["judge_keeps"]["vetoed"], [2])
        self.assertEqual(result["judge_keeps"]["kept"], 0)
        self.assertNotIn("decisions/2.json", self.decisions())
        self.assertNotIn("proposals/2.json", self.proposed())
        self.assertEqual(self.radarr.tag_calls, [])

    def test_a_dry_run_writes_no_tag_deletes_nothing_and_rehearses_the_keep(self):
        code, result = self.execute("dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.radarr.tag_calls, [])
        self.assertEqual(self.radarr.deleted, [])
        self.assertTrue(all(d.get("simulated") for d in result["deleted"]))
        # The keep's decision is ledger state under the dry-run prefix; the delete stays proposed.
        self.assertEqual(self.decisions("dry-run/"), ["dry-run/decisions/2.json"])
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.proposed("dry-run/"), ["dry-run/proposals/1.json", "dry-run/proposals/3.json"])

    def test_tag_keeps_in_dry_run_no_longer_tags_a_judge_keep(self):
        """The flag is for the rating bar; the judge's keep is a decision in every mode."""
        _, result = self.execute("dry-run", flag="true")
        self.assertEqual(self.radarr.tag_calls, [])
        self.assertEqual(result["judge_keeps"]["kept"], 1)
        self.assertEqual(result["keep_tags_added"]["applied"], 0)

    def test_the_rating_bar_still_tags_in_a_dry_run_with_the_flag(self):
        _, result = self.execute("dry-run", flag="true", additions=(4,))
        self.assertEqual(self.radarr.tag_calls, [([4], 1)])
        self.assertEqual(result["keep_tags_added"]["applied"], 1)

    def test_a_keep_inside_its_wait_is_not_settled_in_any_mode(self):
        """The window is the point: a person gets the week to refuse it or keep it for good."""
        for mode, flag in (("act", "false"), ("dry-run", "true")):
            with self.subTest(mode=mode):
                _, result = self.execute(mode, flag=flag, age_days=1)
                self.assertEqual(self.radarr.tag_calls, [])
                self.assertEqual(self.radarr.deleted, [])
                self.assertEqual(result["judge_keeps"]["due"], 0)
                self.assertEqual(self.store.list_keys("decisions/") + self.store.list_keys("dry-run/decisions/"), [])

    def test_a_keep_given_this_run_is_put_on_record_and_not_tagged(self):
        for mode, flag in (("act", "false"), ("dry-run", "true")):
            with self.subTest(mode=mode):
                _, result = self.execute(mode, flag=flag, age_days=1, judged="keep")
                self.assertEqual(self.radarr.tag_calls, [])
                self.assertEqual(result["proposals"]["recorded"], {"delete": 0, "keep": 1})
                self.assertIn(f"{'dry-run/' if mode == 'dry-run' else ''}proposals/4.json", self.store.list_keys(""))

    def test_an_unconfirmed_rating_bar_tag_fails_the_run(self):
        """A rating-bar tag that did not land is tried again; the run must not read as a quiet week."""
        self.radarr.tags_that_do_not_land = {4}
        code, result = self.execute("act", additions=(4,))
        self.assertEqual(code, 8)
        self.assertEqual(result["keep_tags_added"]["unconfirmed"], [4])
        now = datetime.now(timezone.utc)
        plan = {"run_id": "plan-1", "generated_at": now.isoformat(), "counts": {}}
        self.assertIn("1 keep tag not confirmed", reporting.render(plan, result, None, now).push)

    def test_the_digest_says_the_judges_keeps_last_a_year(self):
        _, result = self.execute("act")
        now = datetime.now(timezone.utc)
        plan = {"run_id": "plan-1", "generated_at": now.isoformat(), "counts": {}}
        text = reporting.render(plan, result, None, now).text
        self.assertIn("## Kept for a year by the judge: 1 of 2 due", text)
        self.assertIn(f"Judged again after {result['judge_keeps']['kept_until']}", text)
        self.assertIn("1 marked cleanup-eligible by a person", text)
        self.assertNotIn("Keep tag from the judge", text)


if __name__ == "__main__":
    unittest.main()
