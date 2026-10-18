"""Tests for the archived-app reference check.

Each test builds a small synthetic tree with two archived apps, `oldapp` and `retired`, and asserts what the
check reports. The known-bad controls replay the shape of #6043: a LiteLLM virtual key and its
kustomization entry left behind after the app moved to .archive/.

Run locally:  python3 -m unittest discover -s .github/scripts/tests
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check_archived_app_refs.py"
SPEC = importlib.util.spec_from_file_location("check_archived_app_refs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
# dataclasses resolves the module's annotations through sys.modules (Python 3.12-3.14).
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)

BASE = {
    ".archive/kubernetes/apps/media/oldapp/ks.yaml": "kind: Kustomization\nmetadata:\n  name: oldapp\n",
    ".archive/kubernetes/apps/default/retired/ks.yaml": "kind: Kustomization\nmetadata:\n  name: retired\n",
    "kubernetes/apps/media/kustomization.yaml": "resources:\n  - ./sonarr/ks.yaml\n",
    "kubernetes/apps/media/sonarr/ks.yaml": "kind: Kustomization\nmetadata:\n  name: sonarr\n",
}


class TreeTest(unittest.TestCase):
    """Writes BASE plus per-test files to a temp dir and runs the check over it."""

    def run_check(self, extra: dict[str, str], allowlist: dict[str, str] | None = None) -> tuple[int, str]:
        """Return the check's (exit code, report) for BASE plus extra."""
        files = {**BASE, **extra}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, body in files.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(textwrap.dedent(body), encoding="utf-8")
            return check.run(root, sorted(files), allowlist or {})

    def assert_fails(self, extra: dict[str, str], needle: str) -> None:
        """The check must fail and its report must contain needle."""
        code, report = self.run_check(extra)
        self.assertEqual(code, 1, report)
        self.assertIn(needle, report)

    def assert_passes(self, extra: dict[str, str]) -> None:
        """The check must pass."""
        code, report = self.run_check(extra)
        self.assertEqual(code, 0, report)


class KnownBad(TreeTest):
    """Each leftover shape must fail; these are the controls that prove the check can say no."""

    def test_virtual_key_file_left_behind(self):
        self.assert_fails(
            {"kubernetes/apps/ai/litellm/app/virtualkeys/oldapp.yaml": "kind: LiteLLMVirtualKey\n"},
            "path component 'oldapp'",
        )

    def test_kustomization_entry_left_behind(self):
        self.assert_fails(
            {"kubernetes/apps/ai/litellm/app/virtualkeys/kustomization.yaml": "resources:\n  - ./oldapp.yaml\n"},
            "kustomization resources entry './oldapp.yaml'",
        )

    def test_namespace_kustomization_still_lists_app(self):
        self.assert_fails(
            {"kubernetes/apps/media/kustomization.yaml": "resources:\n  - ./sonarr/ks.yaml\n  - ./oldapp/ks.yaml\n"},
            "kustomization resources entry './oldapp/ks.yaml'",
        )

    def test_depends_on_left_behind(self):
        self.assert_fails(
            {"kubernetes/apps/media/sonarr/ks.yaml": """\
                    kind: Kustomization
                    metadata:
                      name: sonarr
                    spec:
                      dependsOn:
                        - name: onepassword-connect
                          namespace: external-secrets
                        - name: oldapp
                          namespace: media
                    """},
            "dependsOn name 'oldapp'",
        )

    def test_resource_named_for_app_in_other_file(self):
        self.assert_fails(
            {"kubernetes/apps/ai/litellm/app/virtualkeys/keys.yaml": "kind: PushSecret\nmetadata:\n  name: oldapp\n"},
            "PushSecret metadata.name 'oldapp'",
        )

    def test_unparsable_yaml_naming_app(self):
        self.assert_fails(
            {"kubernetes/apps/media/sonarr/app/broken.yaml": "key: [unclosed\nref: oldapp\n"},
            "unparsable YAML",
        )

    def test_stale_allowlist_entry_fails(self):
        code, report = self.run_check({}, {"kubernetes/gone.yaml::oldapp": "was a reason once"})
        self.assertEqual(code, 1, report)
        self.assertIn("match nothing", report)

    def test_no_archived_apps_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, report = check.run(Path(tmp), [], {})
        self.assertEqual(code, 1, report)


class KnownGood(TreeTest):
    """Shapes that must pass: whole-token matching, comments, and the documented skips."""

    def test_clean_tree(self):
        self.assert_passes({})

    def test_name_as_substring_is_not_a_reference(self):
        self.assert_passes(
            {
                "kubernetes/apps/media/oldapp-exporter/ks.yaml": (
                    "kind: Kustomization\nmetadata:\n  name: oldapp-exporter\n"
                ),
                "kubernetes/apps/media/newoldapp/app/oldappx.yaml": "kind: ConfigMap\nmetadata:\n  name: oldapps\n",
                "kubernetes/apps/media/sonarr/app/kustomization.yaml": "resources:\n  - ./oldapp-config.yaml\n",
            }
        )

    def test_commented_out_entry_passes(self):
        self.assert_passes(
            {"kubernetes/apps/media/kustomization.yaml": "resources:\n  - ./sonarr/ks.yaml\n  # - ./oldapp/ks.yaml\n"}
        )

    def test_prose_mention_passes(self):
        self.assert_passes(
            {"kubernetes/apps/media/sonarr/app/helmrelease.yaml": "# replaces oldapp\nkind: HelmRelease\n"}
        )

    def test_unparsable_yaml_with_substring_only_passes(self):
        self.assert_passes({"kubernetes/apps/media/sonarr/app/broken.yaml": "key: [unclosed\nref: oldapp-x\n"})

    def test_name_live_again_is_skipped(self):
        self.assert_passes(
            {
                "kubernetes/apps/ai/oldapp/ks.yaml": "kind: Kustomization\nmetadata:\n  name: oldapp\n",
                "kubernetes/apps/ai/kustomization.yaml": "resources:\n  - ./oldapp/ks.yaml\n",
            }
        )

    def test_archive_dir_without_ks_is_not_an_app(self):
        self.assert_passes(
            {
                ".archive/kubernetes/apps/ai/toolbox/mcp/kustomization.yaml": "resources: []\n",
                "kubernetes/apps/ai/sonarr/app/toolbox.yaml": "kind: ConfigMap\nmetadata:\n  name: toolbox\n",
            }
        )

    def test_allowlisted_hit_passes(self):
        extra = {"kubernetes/apps/ai/litellm/app/virtualkeys/oldapp.yaml": "kind: ConfigMap\n"}
        code, report = self.run_check(
            extra, {"kubernetes/apps/ai/litellm/app/virtualkeys/oldapp.yaml::oldapp": "test reason"}
        )
        self.assertEqual(code, 0, report)

    def test_custom_tags_parse(self):
        self.assert_passes({"kubernetes/apps/media/sonarr/app/tagged.yaml": "value: !Ref something\n"})


if __name__ == "__main__":
    unittest.main()
