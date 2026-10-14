"""One-off: create the decision tags a person uses in the Sonarr UI.

Sonarr here has no import lists, so unlike the film engine there is no per-list
provenance tag to attach: Kometa applies its own tags when it adds a show, and
``FEED_TAGS`` names them. This only creates the three decision tags, and reports
whether each feed tag exists at all -- a feed tag missing from Sonarr means no
series can ever read as feed-added.

This is a configuration change, so it does nothing without ``--apply``.

    python3 -m curatortv.provision            # show what would change
    python3 -m curatortv.provision --apply    # make the change
"""

from __future__ import annotations

import argparse
import os
import sys

from .clients import Sonarr, SourceError
from .model import DEFAULT_FEED_TAGS

DECISION_TAGS = ("cleanup-keep", "cleanup-dismissed", "cleanup-eligible")


def plan_changes(sonarr: Sonarr, feed_tags=DEFAULT_FEED_TAGS) -> tuple[list[str], list[str]]:
    """Decision tags to create, and feed tags Sonarr does not define."""
    existing = {t["label"] for t in sonarr.tags()}
    wanted = [t for t in DECISION_TAGS if t not in existing]
    absent_feeds = sorted(t for t in feed_tags if t not in existing)
    return wanted, absent_feeds


def main(argv: list[str] | None = None) -> int:
    """Create the decision tags."""
    parser = argparse.ArgumentParser(prog="curatortv.provision", description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write the changes")
    args = parser.parse_args(argv)

    sonarr = Sonarr(os.environ["SONARR_URL"], os.environ["SONARR_API_KEY"])
    wanted, absent_feeds = plan_changes(sonarr)

    print(f"tags to create: {wanted or 'none'}")
    if absent_feeds:
        print(f"feed tags Sonarr does not define (no series can read as feed-added): {absent_feeds}")
    if not args.apply:
        print("\ndry run: nothing written. Re-run with --apply to make these changes.")
        return 0

    for label in wanted:
        status, _ = sonarr.create_tag(label)
        print(f"  created {label}: HTTP {status}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SourceError, KeyError) as exc:
        print(f"provisioning failed: {exc}", file=sys.stderr)
        sys.exit(1)
