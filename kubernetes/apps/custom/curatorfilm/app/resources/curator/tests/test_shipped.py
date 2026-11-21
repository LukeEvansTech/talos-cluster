"""Every module of the package has to be in the ConfigMap that ships it.

The code reaches the cluster as a ConfigMap that lists its files by name, so a
module left off the list imports here and fails there, on the first run.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
KUSTOMIZATION = PACKAGE.parents[1] / "kustomization.yaml"


class ShippedModuleTests(unittest.TestCase):
    """The list in the manifest and the files on disk are the same set."""

    def test_every_module_is_listed_and_nothing_listed_is_missing(self):
        text = KUSTOMIZATION.read_text(encoding="utf-8")
        listed = set(re.findall(rf"\./resources/{PACKAGE.name}/([A-Za-z0-9_]+\.py)", text))
        present = {path.name for path in PACKAGE.glob("*.py")}
        self.assertEqual(listed, present)


if __name__ == "__main__":
    unittest.main()
