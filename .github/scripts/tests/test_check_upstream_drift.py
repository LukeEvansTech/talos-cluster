"""Table tests for the upstream-drift classifier, built from the October 2026 misses.

Each row is an image Renovate never raised (it must be flagged) or one it handles (it must pass).
No network: the registry lookup is stubbed with the tags each case publishes.

Run locally:  python3 -m unittest discover -s .github/scripts/tests
"""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check_upstream_drift.py"
SPEC = importlib.util.spec_from_file_location("check_upstream_drift", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
drift = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drift)

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
FRESH = {"created": "2026-10-01T00:00:00Z", "labels": {}}


def info(
    repo: str,
    tag: str,
    published: str = "2026-09-01T00:00:00Z",
    archived: bool = False,
) -> dict:
    """Upstream repository info as the GraphQL lookup returns it."""
    release = {"tagName": tag, "publishedAt": published}
    return {"nameWithOwner": repo, "isArchived": archived, "latestRelease": release}


# (name, image, tag, meta, source repo, upstream info, tags published, expected substring or None)
CASES = [
    (
        "beta suffix hides stable line",
        "ghcr.io/o/reclaimerr",
        "0.1.0-beta7",
        FRESH,
        "o/reclaimerr",
        info("o/reclaimerr", "0.5.6"),
        set(),
        "not published as",
    ),
    (
        "two-part tags hide a major",
        "ghcr.io/o/jellyfin",
        "10.11.11",
        FRESH,
        "o/jellyfin",
        info("o/jellyfin", "v12.2"),
        {"12.2"},
        "not published as",
    ),
    (
        "org move",
        "ghcr.io/old/maintainerr",
        "2.19.0",
        FRESH,
        "old/maintainerr",
        info("New/Maintainerr", "v3.30.1"),
        set(),
        "moved to New/Maintainerr",
    ),
    (
        "prefix Renovate cannot parse",
        "ghcr.io/o/termix",
        "release-1.10.0",
        FRESH,
        "o/termix",
        info("o/termix", "v26.10.1"),
        set(),
        "not published as",
    ),
    ("floating latest", "docker.io/o/chrome", "latest", FRESH, None, None, set(), "floating tag"),
    (
        "floating branch",
        "ghcr.io/o/litellm",
        "main-stable",
        FRESH,
        "o/litellm",
        info("o/litellm", "v1.105.0"),
        set(),
        "floating tag",
    ),
    (
        "commit SHA",
        "ghcr.io/o/plugin",
        "36bfc606bba2064de6ede0ff2764cbb52edff70d",
        FRESH,
        "o/plugin",
        None,
        set(),
        "floating tag",
    ),
    (
        "registry gone",
        "ghcr.io/o/game",
        "v1.9.10",
        {"error": "registry answered 401"},
        "o/game",
        None,
        set(),
        "cannot be pulled",
    ),
    (
        "same shape, Renovate will raise it",
        "ghcr.io/o/app",
        "1.2.3",
        FRESH,
        "o/app",
        info("o/app", "v1.2.4", "2026-10-10T00:00:00Z"),
        set(),
        None,
    ),
    (
        "suffix kept on new version",
        "ghcr.io/o/scrutiny",
        "1.76.4-omnibus",
        FRESH,
        "o/scrutiny",
        info("o/scrutiny", "v1.76.5", "2026-10-10T00:00:00Z"),
        {"1.76.5-omnibus"},
        None,
    ),
    (
        "two-part tag that upstream also publishes",
        "ghcr.io/o/keeper",
        "2.24",
        FRESH,
        "o/keeper",
        info("o/keeper", "v2.25.0", "2026-10-10T00:00:00Z"),
        {"2.25"},
        None,
    ),
    (
        "archived repo, image still built from the monorepo",
        "ghcr.io/o/actual-server",
        "26.10.0",
        FRESH,
        "o/actual-server",
        info("o/actual-server", "v25.2.0", archived=True),
        set(),
        None,
    ),
    ("current", "ghcr.io/o/app", "2.0.0", FRESH, "o/app", info("o/app", "2.0.0"), set(), None),
    (
        "two-part tag already current",
        "ghcr.io/o/keeper",
        "2.25",
        FRESH,
        "o/keeper",
        info("o/keeper", "v2.25.0"),
        {"2.25"},
        None,
    ),
    (
        "prerelease of the version now released stable",
        "ghcr.io/o/app",
        "1.0.0-beta7",
        FRESH,
        "o/app",
        info("o/app", "v1.0.0"),
        {"1.0.0-beta7"},
        "not published as",
    ),
    (
        "old rename, image still current",
        "ghcr.io/o/miniflux",
        "2.3.3-distroless",
        FRESH,
        "o/miniflux",
        info("o/v2", "2.3.3"),
        set(),
        None,
    ),
    (
        "short floating track still published",
        "ghcr.io/o/jellyfin",
        "12.2",
        FRESH,
        "o/jellyfin",
        info("o/jellyfin", "v12.2.1", "2026-10-10T00:00:00Z"),
        {"12.2"},
        None,
    ),
    (
        "calendar tags to semver",
        "docker.io/o/hermes-agent",
        "v2026.8.16.2",
        FRESH,
        "o/hermes-agent",
        info("o/hermes-agent", "v0.21.6"),
        set(),
        "switched version scheme",
    ),
]


class AssessTest(unittest.TestCase):
    """assess() against the table above, plus the two staleness edge cases."""

    def test_cases(self) -> None:
        """Each row is flagged with its expected reason, or passes clean."""
        for name, image, tag, meta, repo, upstream, published, expected in CASES:
            with self.subTest(name):
                setattr(drift, "tag_exists", lambda _image, t, published=published: t in published)
                reasons = "; ".join(drift.assess(image, tag, meta, (repo, upstream), NOW))
                if expected is None:
                    self.assertEqual(reasons, "")
                else:
                    self.assertIn(expected, reasons)

    def test_stale_image_is_watch_only(self) -> None:
        """An old build with nothing else wrong yields a single watch-only reason."""
        old = {"created": "2024-02-16T00:00:00Z", "labels": {}}
        reasons = drift.assess("ghcr.io/o/x", "1.0.0", old, ("o/x", info("o/x", "1.0.0")), NOW)
        self.assertEqual(len(reasons), 1)
        self.assertTrue(reasons[0].startswith(drift.WATCH))

    def test_extracts_every_declaration_shape(self) -> None:
        """Comment lines between repository and tag, and CNPG `imageName`, are still found."""
        text = (
            "      image:\n        repository: ghcr.io/o/scrypted\n"
            "        # pinned for a reason\n        tag: v0.150.0@sha256:abc\n"
            "  imageName: ghcr.io/cloudnative-pg/postgresql:18.6@sha256:abc\n"
            "        image: ghcr.io/o/side:1.2.3\n"
            "        repository: ghcr.io/${APP}/x\n        tag: 1.0.0\n"
            "        image: homebridge/homebridge:2026-09-25\n"
        )
        self.assertEqual(
            sorted(drift.extract(text)),
            [
                ("ghcr.io/cloudnative-pg/postgresql", "18.6"),
                ("ghcr.io/o/scrypted", "v0.150.0"),
                ("ghcr.io/o/side", "1.2.3"),
                ("homebridge/homebridge", "2026-09-25"),
            ],
        )

    def test_pending_matches_the_exact_tag_track(self) -> None:
        """A pending 3.15-alpine update must not cover the separate 3.15 track."""
        pending = ["update docker.io/library/python (3.15-alpine ➔ 3.16-alpine)"]
        self.assertTrue(drift.is_pending("docker.io/library/python", "3.15-alpine", pending))
        self.assertFalse(drift.is_pending("docker.io/library/python", "3.15", pending))

    def test_label_from_another_project_is_not_trusted(self) -> None:
        """A base image's source label must not stand in for the app's own repository."""
        labelled = {"labels": {"org.opencontainers.image.source": "https://github.com/astral-sh/uv"}}
        self.assertEqual(drift.source_repo("ghcr.io/wizarrrr/wizarr", labelled), "wizarrrr/wizarr")
        own = {"labels": {"org.opencontainers.image.source": "https://github.com/Sonarr/Sonarr"}}
        self.assertEqual(drift.source_repo("ghcr.io/home-operations/sonarr", own), "Sonarr/Sonarr")

    def test_epoch_build_time_is_not_stale(self) -> None:
        """Reproducible builds stamp the epoch, which must not read as age."""
        self.assertIsNone(drift.age_days("1970-01-01T00:00:01Z", NOW))


if __name__ == "__main__":
    unittest.main()
