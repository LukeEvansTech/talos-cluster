#!/usr/bin/env python3
"""Fail if a homepage tile icon is not a generated Dracula gradient SVG.

A plain `mdi-*` icon renders white beside the purple-to-pink tiles; six Custom tiles had drifted
that way before this check existed. Every Flux Kustomization that uses the homepage component
must set HOMEPAGE_ICON to `/icons/dracula/<slug>.svg`, and that slug needs its SVG, its entry in
the homepage kustomization's configMapGenerator and its row in icons/sources.tsv, so that
`uv run scripts/homepage_icons.py <slug>` can regenerate it.
"""

from __future__ import annotations

import pathlib
import re
import sys

HOMEPAGE = pathlib.Path("kubernetes/apps/default/homepage/app")
COMPONENT = pathlib.Path("kubernetes/components/homepage/kustomization.yaml")
ICON_RE = re.compile(r"^\s*HOMEPAGE_ICON:\s*(.*?)\s*(?:\s#.*)?$")
DEFAULT_RE = re.compile(r"\$\{HOMEPAGE_ICON:=([^}]+)\}")
DRACULA_RE = re.compile(r"^/icons/dracula/([a-z0-9-]+)\.svg$")
FIX = "fix: add a sources.tsv row, run `uv run scripts/homepage_icons.py <slug>`, list icons/<slug>.svg"


def scalar(raw: str) -> str:
    """The whole YAML scalar with one pair of matching quotes removed, so trailing text is kept."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def check(root: pathlib.Path) -> list[str]:
    """Return one problem line per bad icon reference under root."""
    homepage = root / HOMEPAGE
    listed = set(
        re.findall(r"^\s*-\s*icons/([a-z0-9-]+)\.svg\s*$", (homepage / "kustomization.yaml").read_text(), re.M)
    )
    sources = {
        line.split("\t")[0]
        for line in (homepage / "icons/sources.tsv").read_text().splitlines()
        if line and not line.startswith("#")
    }

    refs: list[tuple[str, str]] = []
    problems: list[str] = []
    for ks in sorted((root / "kubernetes").rglob("ks.yaml")):
        text = ks.read_text()
        rel = ks.relative_to(root).as_posix()
        icons = [scalar(m.group(1)) for line in text.splitlines() if (m := ICON_RE.match(line))]
        if "components/homepage" in text and not icons:
            problems.append(f"{rel}: uses the homepage component but sets no HOMEPAGE_ICON")
        refs += [(rel, icon) for icon in icons]
    component = root / COMPONENT
    refs += [(COMPONENT.as_posix(), m) for m in DEFAULT_RE.findall(component.read_text())]

    for where, icon in refs:
        m = DRACULA_RE.match(icon)
        if not m:
            problems.append(f"{where}: icon {icon!r} is not /icons/dracula/<slug>.svg ({FIX})")
            continue
        slug = m.group(1)
        if not (homepage / f"icons/{slug}.svg").is_file():
            problems.append(f"{where}: icons/{slug}.svg does not exist ({FIX})")
        if slug not in listed:
            problems.append(f"{where}: icons/{slug}.svg is not listed in {HOMEPAGE}/kustomization.yaml")
        if slug not in sources:
            problems.append(f"{where}: {slug} has no row in {HOMEPAGE}/icons/sources.tsv")
    return problems


def main() -> int:
    """Print every bad icon reference; exit 1 if there are any."""
    problems = check(pathlib.Path("."))
    for p in problems:
        print(p)
    if problems:
        return 1
    print("homepage-icons: every tile icon is a generated Dracula SVG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
