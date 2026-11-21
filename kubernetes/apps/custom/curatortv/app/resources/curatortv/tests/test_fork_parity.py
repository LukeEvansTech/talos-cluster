"""The modules curatortv shares with the film engine must stay identical.

curatortv is a fork of curator, and a fix landing in one engine has twice been
missing from the other (#6185, #6208). These modules hold no film- or
series-specific logic, so any difference is drift.
"""

from __future__ import annotations

import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
# Globbed across namespaces, so the two engines need not share one.
FILM = next(
    HERE.parents[4].glob("*/curator/app/resources/curator"),
    HERE.parents[3] / "curator" / "app" / "resources" / "curator",
)
SHARED = ("s3.py", "settings.py", "store.py")
# The two judge apps ship these unchanged: the id field reaches them in the batch list.
JUDGE_SHARED = ("judge.py", "read_verdicts.py")


class SharedModulesMatchTheFilmEngine(unittest.TestCase):
    """A drifted shared module fails here, naming the file to reconcile."""

    def test_the_film_engine_is_where_this_expects(self):
        self.assertTrue((FILM / "__init__.py").is_file(), FILM)

    def test_shared_modules_are_identical(self):
        for name in SHARED:
            with self.subTest(module=name):
                tv = (HERE / name).read_text(encoding="utf-8").replace("curatortv", "curator")
                film = (FILM / name).read_text(encoding="utf-8")
                self.assertEqual(tv, film, f"{name} differs between curatortv and curator")

    def test_shared_judge_scripts_are_identical(self):
        film_judge = FILM.parents[3] / "curatorjudge" / "app" / "resources"
        tv_judge = HERE.parents[3] / "curatortvjudge" / "app" / "resources"
        for name in JUDGE_SHARED:
            with self.subTest(script=name):
                tv = (tv_judge / name).read_text(encoding="utf-8")
                film = (film_judge / name).read_text(encoding="utf-8")
                self.assertEqual(tv, film, f"{name} differs between curatortvjudge and curatorjudge")


if __name__ == "__main__":
    unittest.main()
