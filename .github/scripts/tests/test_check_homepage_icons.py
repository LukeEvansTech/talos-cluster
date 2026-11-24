"""Tests for the homepage icon check.

Each test builds a small synthetic tree and asserts what the check reports. The known-bad controls
replay #7705: tiles set to a plain `mdi-*` icon, which renders white.

Run locally:  python3 -m unittest discover -s .github/scripts/tests
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check_homepage_icons.py"
SPEC = importlib.util.spec_from_file_location("check_homepage_icons", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)

HP = "kubernetes/apps/default/homepage/app"
GENERATORS = (
    "configMapGenerator:\n  - name: homepage-config\n    files:\n      - settings.yaml=./resources/settings.yaml\n"
    "  - name: homepage-icons\n    files:\n      - icons/application.svg\n      - icons/good.svg\n"
)
COMPONENT = 'gethomepage.dev/icon: "${HOMEPAGE_ICON:=/icons/dracula/application.svg}"\n'


def ks(icon: str | None) -> str:
    """A ks.yaml using the homepage component, with an optional HOMEPAGE_ICON."""
    line = f"      HOMEPAGE_ICON: {icon}\n" if icon else ""
    return f"spec:\n  components:\n    - ../../../../components/homepage\n  postBuild:\n    substitute:\n{line}"


class HomepageIconTests(unittest.TestCase):
    """Synthetic-tree cases for check()."""

    def run_check(self, files: dict[str, str]) -> list[str]:
        """Write the base tree plus files into a temp dir and run the check there."""
        base = {
            f"{HP}/kustomization.yaml": GENERATORS,
            f"{HP}/icons/sources.tsv": (
                "# slug\tmode\tthreshold\tsource\napplication\talpha\t0.35\tu\ngood\talpha\t0.35\tu\n"
            ),
            f"{HP}/icons/application.svg": "<svg/>",
            f"{HP}/icons/good.svg": "<svg/>",
            "kubernetes/components/homepage/kustomization.yaml": COMPONENT,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, body in {**base, **files}.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(body)
            return check.check(root)

    def test_generated_icon_passes(self):
        self.assertEqual(self.run_check({"kubernetes/apps/x/good/ks.yaml": ks("/icons/dracula/good.svg")}), [])

    def test_mdi_icon_fails(self):
        out = self.run_check({"kubernetes/apps/custom/curator/ks.yaml": ks("mdi-broom")})
        self.assertEqual(len(out), 1)
        self.assertIn("'mdi-broom' is not /icons/dracula/", out[0])

    def test_mdi_component_default_fails(self):
        out = self.run_check(
            {
                "kubernetes/components/homepage/kustomization.yaml": COMPONENT.replace(
                    "/icons/dracula/application.svg", "mdi-application"
                )
            }
        )
        self.assertEqual(len(out), 1)
        self.assertIn("kubernetes/components/homepage/kustomization.yaml", out[0])

    def test_missing_icon_fails(self):
        out = self.run_check({"kubernetes/apps/x/app/ks.yaml": ks(None)})
        self.assertEqual(out, ["kubernetes/apps/x/app/ks.yaml: uses the homepage component but sets no HOMEPAGE_ICON"])

    def test_unregistered_slug_reports_every_gap(self):
        out = self.run_check({"kubernetes/apps/x/new/ks.yaml": ks("/icons/dracula/new.svg")})
        self.assertEqual(len(out), 3)
        self.assertTrue(any("does not exist" in o for o in out))
        self.assertTrue(any("homepage-icons generator" in o for o in out))
        self.assertTrue(any("no row" in o for o in out))

    def test_quoted_value_passes(self):
        for icon in ("'/icons/dracula/good.svg'", '"/icons/dracula/good.svg"'):
            self.assertEqual(self.run_check({"kubernetes/apps/x/good/ks.yaml": ks(icon)}), [], icon)

    def test_trailing_text_fails(self):
        out = self.run_check({"kubernetes/apps/x/good/ks.yaml": ks('"/icons/dracula/good.svg extra"')})
        self.assertEqual(len(out), 1)

    def test_direct_annotation_is_checked(self):
        route = "metadata:\n  annotations:\n    gethomepage.dev/icon: mdi-git\n"
        out = self.run_check({"kubernetes/apps/x/forgejo/app/httproute.yaml": route})
        self.assertEqual(len(out), 1)
        self.assertIn("httproute.yaml:3", out[0])

    def test_templated_annotation_default_is_checked(self):
        route = "metadata:\n  annotations:\n    gethomepage.dev/icon: ${HOMEPAGE_ICON:=mdi-git}\n"
        out = self.run_check({"kubernetes/apps/x/forgejo/app/httproute.yaml": route})
        self.assertEqual(len(out), 1)
        self.assertIn("'mdi-git'", out[0])

    def test_templated_annotation_without_default_fails(self):
        route = "metadata:\n  annotations:\n    gethomepage.dev/icon: ${HOMEPAGE_ICON}\n"
        out = self.run_check({"kubernetes/apps/x/forgejo/app/httproute.yaml": route})
        self.assertEqual(len(out), 1)
        self.assertIn("is not exactly", out[0])

    def test_templated_annotation_with_suffix_fails(self):
        route = "metadata:\n  annotations:\n    gethomepage.dev/icon: ${HOMEPAGE_ICON:=/icons/dracula/good.svg}.png\n"
        out = self.run_check({"kubernetes/apps/x/forgejo/app/httproute.yaml": route})
        self.assertEqual(len(out), 1)
        self.assertIn("is not exactly", out[0])

    def test_icon_in_other_generator_fails(self):
        moved = GENERATORS.replace("      - icons/good.svg\n", "").replace(
            "./resources/settings.yaml\n", "./resources/settings.yaml\n      - icons/good.svg\n"
        )
        out = self.run_check(
            {f"{HP}/kustomization.yaml": moved, "kubernetes/apps/x/good/ks.yaml": ks("/icons/dracula/good.svg")}
        )
        self.assertEqual(len(out), 1)
        self.assertIn("homepage-icons generator", out[0])

    def test_ks_without_component_is_ignored(self):
        self.assertEqual(self.run_check({"kubernetes/apps/x/plain/ks.yaml": "spec:\n  interval: 1h\n"}), [])


if __name__ == "__main__":
    unittest.main()
