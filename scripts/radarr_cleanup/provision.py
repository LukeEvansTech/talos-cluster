"""One-off: give each import list a provenance tag so future additions are attributable.

Radarr does not record which import list added a film. It does apply a list's tags
to everything that list adds, which makes a per-list tag the only durable evidence
of automatic origin available in this stack.

This is a configuration change, so it does nothing without ``--apply``. It is also
**not retrospective**: films already in the library keep their unknown origin
forever, and the only way to authorise one of those for cleanup is the
``cleanup-eligible`` tag applied by a person.

    python3 -m radarr_cleanup.provision            # show what would change
    python3 -m radarr_cleanup.provision --apply    # make the change
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from .clients import Radarr, SourceError
from .model import PROVENANCE_TAG_PREFIX

DECISION_TAGS = ("cleanup-keep", "cleanup-dismissed", "cleanup-eligible")


def slug(name: str) -> str:
    """A stable, readable tag suffix for an import list name."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return cleaned[:40] or "list"


def plan_changes(radarr: Radarr) -> tuple[list[dict], list[str]]:
    """Work out which tags are missing and which lists need one."""
    existing = {t["label"]: t["id"] for t in radarr.tags()}
    lists = radarr.import_lists()

    wanted_tags: list[str] = [t for t in DECISION_TAGS if t not in existing]
    changes: list[dict] = []
    for source in lists:
        label = f"{PROVENANCE_TAG_PREFIX}{slug(source['name'])}"
        if label not in existing and label not in wanted_tags:
            wanted_tags.append(label)
        if existing.get(label) not in (source.get("tags") or []):
            changes.append({"list_id": source["id"], "list_name": source["name"], "tag": label})
    return changes, wanted_tags


def main(argv: list[str] | None = None) -> int:
    """Create the tags and attach them to their import lists."""
    parser = argparse.ArgumentParser(prog="radarr_cleanup.provision", description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write the changes")
    args = parser.parse_args(argv)

    radarr = Radarr(os.environ["RADARR_URL"], os.environ["RADARR_API_KEY"])
    changes, wanted_tags = plan_changes(radarr)

    print(f"tags to create: {wanted_tags or 'none'}")
    for change in changes:
        print(f"  list {change['list_id']} ({change['list_name']}) -> {change['tag']}")
    if not args.apply:
        print("\ndry run: nothing written. Re-run with --apply to make these changes.")
        print("Note this is NOT retrospective -- only films added after this point get a tag.")
        return 0

    for label in wanted_tags:
        status, _ = radarr.create_tag(label)
        print(f"  created {label}: HTTP {status}")

    tag_ids = {t["label"]: t["id"] for t in radarr.tags()}
    lists = {source["id"]: source for source in radarr.import_lists()}
    for change in changes:
        source = lists[change["list_id"]]
        tag_id = tag_ids[change["tag"]]
        source["tags"] = sorted(set(source.get("tags") or []) | {tag_id})
        status, _ = radarr.http.request_json(
            "PUT", f"/api/v3/importlist/{source['id']}", radarr.headers, source
        )
        print(f"  list {source['id']} tagged {change['tag']}: HTTP {status}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SourceError, KeyError) as exc:
        print(f"provisioning failed: {exc}", file=sys.stderr)
        sys.exit(1)
