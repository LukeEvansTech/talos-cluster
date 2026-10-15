"""The modules curatortv shares with the film engine must stay identical.

curatortv is a fork of curator, and a fix landing in one engine has twice been
missing from the other (#6185, #6208). These modules hold no film- or
series-specific logic, so any difference is drift.
"""

from __future__ import annotations

import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
FILM = HERE.parents[3] / "curator" / "app" / "resources" / "curator"
SHARED = ("s3.py", "store.py")


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


if __name__ == "__main__":
    unittest.main()
