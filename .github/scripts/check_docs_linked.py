#!/usr/bin/env python3
"""Fail if a docs page is unreachable from the site navigation.

zensical builds a page that no nav entry names without complaint, even with
--strict, so a page added beside a manifest change is reachable only through
search. Every page under docs/docs/ must appear in docs/zensical.toml, and every
KB entry must also be linked from the troubleshooting index.

Run locally:  python3 .github/scripts/check_docs_linked.py
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

DOCS = Path("docs/docs")
NAV = Path("docs/zensical.toml")
INDEX = DOCS / "troubleshooting/index.md"
# Pages deliberately outside the nav: the landing page is the site root.
UNLISTED = {"index.md"}


def nav_pages(node: object) -> set[str]:
    """Return every page path named anywhere in a parsed nav tree."""
    if isinstance(node, str):
        return {node}
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    found: set[str] = set()
    for child in children:
        found |= nav_pages(child)
    return found


def main() -> int:
    """Report every page missing from the nav and every KB entry missing from the index."""
    config = tomllib.loads(NAV.read_text(encoding="utf-8"))
    nav = nav_pages(config.get("project", config).get("nav", []))
    index = INDEX.read_text(encoding="utf-8")
    problems: list[str] = []
    for page in sorted(DOCS.rglob("*.md")):
        rel = page.relative_to(DOCS).as_posix()
        if rel in UNLISTED:
            continue
        if rel not in nav:
            problems.append(f"{rel}: not in {NAV} nav")
        if rel.startswith("troubleshooting/kb/") and page.name not in index:
            problems.append(f"{rel}: not linked from {INDEX}")
    if problems:
        print("Docs pages unreachable from the site navigation:\n")
        print("\n".join(f"  {p}" for p in problems))
        print("\nAdd each page to docs/zensical.toml; add each KB entry to the symptom ladder too.")
        return 1
    print("OK -- every docs page is in the nav and every KB entry is indexed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
