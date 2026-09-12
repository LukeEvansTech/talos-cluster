"""Evidence that outlived the moment it was gathered.

Two shapes, both of which read as a decision that was made this week. A file on
a volume that survives from one run to the next, left in place by a step that
died before rewriting it; and a snapshot taken before a batch of deletions began
and still being consulted several minutes and several deletions later.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from curator import __main__ as cli
from curator.clients import SourceError
from curator.executor import execute, volatile_block
from curator.model import Assessment, Outcome

from .fixtures import (
    NOW,
    FakeRadarr,
    FakeTautulli,
    feed_provenance,
    make_availability,
    make_film,
    make_viewing,
)

def _judge_script() -> Path:
    """The judge's validator, when these tests run from a checkout.

    Walked rather than indexed off ``parents``: in-cluster this package is
    mounted at /app/curator, which is four levels deep, and a fixed index raised
    an IndexError at import time -- taking the whole selftest with it.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "curatorjudge" / "app" / "resources" / "read_verdicts.py"
        if candidate.is_file():
            return candidate
    return here.with_name("read_verdicts-not-mounted.py")


JUDGE_SCRIPT = _judge_script()

PLANNED_AT = NOW - timedelta(minutes=30)


def load_read_verdicts(work: Path, state: Path) -> Any:
    """Import the judge's validator with its paths pointed at a temp directory."""
    spec: Any = importlib.util.spec_from_file_location("read_verdicts_under_test", JUDGE_SCRIPT)
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.WORK = work
    module.PLAN = state / "plan.json"
    module.OUT = state / "recommendations.json"
    module.RECORD = state / "judgement.json"
    return module


@unittest.skipUnless(JUDGE_SCRIPT.is_file(), "judge scripts are not mounted in-cluster")
class LeftoverRecommendationsTests(unittest.TestCase):
    """Last week's verdicts must not survive a judging step that failed."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.work = root / "work"
        self.state = root / "state"
        self.work.mkdir()
        self.state.mkdir()
        (self.state / "plan.json").write_text(json.dumps({"run_id": "this-week"}), encoding="utf-8")
        # What a previous, successful week left behind.
        (self.state / "recommendations.json").write_text(
            json.dumps({"plan_run_id": "last-week", "recommendations": [{"movie_id": 42, "verdict": "delete"}]}),
            encoding="utf-8",
        )
        (self.state / "judgement.json").write_text(
            json.dumps({"status": "judged", "detail": "1 recommendations", "plan_run_id": "last-week"}),
            encoding="utf-8",
        )

    def recommendations(self) -> dict:
        """The file as the engine would read it."""
        return json.loads((self.state / "recommendations.json").read_text(encoding="utf-8"))

    def judgement(self) -> dict:
        """The account of what happened."""
        return json.loads((self.state / "judgement.json").read_text(encoding="utf-8"))

    def test_an_envelope_of_the_wrong_type_is_reported_not_crashed_through(self):
        """A bare JSON array where an object was expected used to raise at .get()."""
        (self.work / "claude.json").write_text("[]", encoding="utf-8")
        module = load_read_verdicts(self.work, self.state)
        self.assertEqual(module.main(), 1)
        self.assertIn("not an object", self.judgement()["detail"])

    def test_last_weeks_verdicts_do_not_survive_a_failure(self):
        (self.work / "claude.json").write_text("[]", encoding="utf-8")
        module = load_read_verdicts(self.work, self.state)
        module.main()
        self.assertEqual(self.recommendations()["recommendations"], [])

    def test_the_refusal_is_written_before_the_answer_is_parsed(self):
        """The seal is what a crash anywhere below leaves behind."""
        module = load_read_verdicts(self.work, self.state)
        module.write_state(module.plan_run_id(), "failed", "read_verdicts did not finish")
        self.assertEqual(self.recommendations()["recommendations"], [])
        self.assertEqual(self.recommendations()["plan_run_id"], "this-week")
        self.assertEqual(self.judgement()["status"], "failed")

    def test_an_unreadable_plan_yields_a_run_id_that_matches_nothing(self):
        (self.state / "plan.json").write_text("{ truncated", encoding="utf-8")
        module = load_read_verdicts(self.work, self.state)
        self.assertIsNone(module.plan_run_id())

    def test_a_good_answer_names_the_plan_it_judged(self):
        (self.work / "claude.json").write_text(
            json.dumps(
                {
                    "is_error": False,
                    "duration_ms": 900,
                    "result": json.dumps([{"movie_id": 7, "verdict": "delete", "reason": "feed filler, no plays"}]),
                }
            ),
            encoding="utf-8",
        )
        module = load_read_verdicts(self.work, self.state)
        self.assertEqual(module.main(), 0)
        payload = self.recommendations()
        self.assertEqual(payload["plan_run_id"], "this-week")
        self.assertEqual(len(payload["recommendations"]), 1)
        self.assertEqual(self.judgement()["verdicts"], {"delete": 1})


class EngineRefusesLeftoversTests(unittest.TestCase):
    """The engine is the second place this is caught, and the last one."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "plan.json").write_text(json.dumps({"run_id": "this-week"}), encoding="utf-8")
        self.addCleanup(setattr, cli, "build_sources", cli.build_sources)
        cli.build_sources = _sources

    def run_execute(self, payload) -> int:
        """Run the execute command against a recommendations file."""
        path = self.root / "recommendations.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        args = argparse.Namespace(
            plan=str(self.root / "plan.json"), recommendations=str(path), read_only=True, out=None
        )
        return cli.cmd_execute(args)

    def test_verdicts_judged_against_another_plan_are_refused(self):
        code = self.run_execute({"plan_run_id": "last-week", "recommendations": [{"movie_id": 42}]})
        self.assertEqual(code, 7)

    def test_verdicts_with_no_plan_named_are_refused(self):
        """Every file this pipeline writes names its plan. One that does not is not ours."""
        self.assertEqual(self.run_execute({"recommendations": [{"movie_id": 42}]}), 7)

    def test_this_weeks_verdicts_get_past_the_guard(self):
        self.assertNotEqual(self.run_execute({"plan_run_id": "this-week", "recommendations": []}), 7)


def _sources() -> SimpleNamespace:
    """Just enough of build_sources() for the guard above it to run."""
    return SimpleNamespace(radarr=FakeRadarr({}), ledger=SimpleNamespace(dry_run=True), run_id="run-execute")


class VolatileEvidenceTests(unittest.TestCase):
    """A snapshot taken before the batch is not evidence about its last film."""

    def film(self):
        """The film being considered for deletion."""
        return make_film(movie_id=1, tmdb_id=1000, imdb_id="tt0000001")

    def test_a_play_that_started_during_the_run_blocks(self):
        tautulli = FakeTautulli(playing_ids={("tmdb", "1000")})
        self.assertEqual(
            volatile_block(self.film(), tautulli, None, {}, PLANNED_AT),
            "somebody started watching it during this run",
        )

    def test_an_unreachable_activity_check_blocks(self):
        tautulli = FakeTautulli(raise_error=SourceError("down"))
        self.assertIn("could not re-confirm", volatile_block(self.film(), tautulli, None, {}, PLANNED_AT) or "")

    def test_history_that_cannot_be_re_read_blocks(self):
        tautulli = FakeTautulli()
        tautulli.history_complete = False
        self.assertEqual(
            volatile_block(self.film(), tautulli, None, {}, PLANNED_AT),
            "could not re-read plays and requests during this run",
        )

    def test_a_quiet_library_blocks_nothing(self):
        self.assertIsNone(volatile_block(self.film(), FakeTautulli(), None, {}, PLANNED_AT))


class BatchDriftTests(unittest.TestCase):
    """The check runs per film, not once before thirty deletions."""

    def library(self) -> dict[int, dict]:
        """Two deletable films, live in Radarr."""
        return {
            movie_id: {
                "id": movie_id,
                "title": f"Film {movie_id}",
                "tmdbId": 1000 + movie_id,
                "imdbId": f"tt000000{movie_id}",
                "status": "released",
                "hasFile": True,
                "tags": [],
            }
            for movie_id in (1, 2)
        }

    def approved(self):
        """Assessments for both films, in a fixed order."""
        return [
            Assessment(
                film=make_film(movie_id=movie_id, tmdb_id=1000 + movie_id, imdb_id=f"tt000000{movie_id}"),
                outcome=Outcome.CANDIDATE,
                provenance=feed_provenance(),
                availability=make_availability(),
                viewing=make_viewing(),
            )
            for movie_id in (1, 2)
        ]

    def test_a_play_starting_mid_batch_spares_the_later_film(self):
        tautulli = FakeTautulli()
        # Nobody is watching when the batch starts and when film 1 is checked;
        # by the time film 2 is reached, somebody has pressed play on it.
        tautulli.playing_ids_sequence = [set(), set(), {("tmdb", "1002")}]
        result = execute(
            self.approved(),
            FakeRadarr(self.library()),
            tautulli,
            _Ledger(),
            tag_labels={},
            monitored_collection_tmdb_ids=set(),
            dry_run=True,
            crosswalk={},
            planned_at=PLANNED_AT,
        )
        self.assertEqual([d["movie_id"] for d in result.deleted], [1])
        self.assertEqual(
            [(s["movie_id"], s["why"]) for s in result.skipped],
            [(2, "somebody started watching it during this run")],
        )
        # Once before the loop and once per film. A single pre-loop read is
        # what let the second deletion proceed on the first one's evidence.
        self.assertEqual(tautulli.now_playing_calls, 3)


class _Ledger:
    """Records intent without persisting anything."""

    dry_run = True

    def __init__(self):
        self.intents: list[dict] = []

    def record_intent(self, record, assessment):
        """Remember the intent."""
        self.intents.append({"record": record, "assessment": assessment})

    def record_outcome(self, movie_id, status, detail=""):
        """Ignore the outcome; these tests never delete."""


if __name__ == "__main__":
    unittest.main()
