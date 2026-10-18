#!/usr/bin/env python3
"""Fail if anything under kubernetes/ still references an archived app as a resource.

Archiving an app (AGENTS.md "Archiving an app") moves it to .archive/, but the
app usually has resources living in OTHER apps' directories: a LiteLLM virtual
key and its PushSecret, a dependsOn, a kustomization entry. Archiving
recommendarr in #6043 needed three such edits outside its own directory, and
nothing would have caught a missed one. The full runbook is
docs/docs/operations/retiring-an-app.md; this is its checkable step.

An archived app is a directory .archive/kubernetes/apps/<ns>/<app> holding a
ks.yaml. Directories without one (partial archives such as retired MCP server
manifests of an app that is still live) are not apps and are skipped, as is an
archived name that is live again under kubernetes/apps/*/<app>/ks.yaml.

For each archived app name, a tracked file under kubernetes/ fails when:
  - a path component of the file equals the name, or does once its extension
    is dropped (virtualkeys/<app>.yaml);
  - its kustomization.yaml lists a resources/components entry with such a
    path component;
  - any dependsOn entry in it has `name: <app>`;
  - a document in it has `metadata.name: <app>`.

Matching is whole-token: `sonarr` never matches `sonarr-exporter`. Comments are
not parsed, so a commented-out `# - ./<app>/ks.yaml` line passes.

ALLOWLIST accepts a hit by "path::app" with a reason. An entry that matches
nothing fails the run, so a stale entry cannot silently accept a later hit.

Run locally:  python3 .github/scripts/check_archived_app_refs.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml  # pylint: disable=import-error  # on the runner image; not in the linter's env

ARCHIVE_PREFIX = (".archive", "kubernetes", "apps")
LIVE_PREFIX = ("kubernetes", "apps")

# "path::app": reason
ALLOWLIST: dict[str, str] = {}


# Tolerate the custom tags some manifests carry instead of refusing the whole file.
yaml.SafeLoader.add_multi_constructor("", lambda _loader, _suffix, _node: None)


@dataclass(frozen=True)
class Hit:
    """One reference to an archived app."""

    path: str
    app: str
    reason: str

    @property
    def key(self) -> str:
        """The ALLOWLIST key for this hit."""
        return f"{self.path}::{self.app}"


def _app_dirs(paths: list[str], prefix: tuple[str, ...]) -> dict[str, str]:
    """Map app name -> "<ns>/<app>" for every <prefix>/<ns>/<app>/ks.yaml in paths."""
    found: dict[str, str] = {}
    n = len(prefix)
    for p in paths:
        parts = PurePosixPath(p).parts
        if len(parts) == n + 3 and parts[:n] == prefix and parts[-1] == "ks.yaml":
            found.setdefault(parts[n + 1], f"{parts[n]}/{parts[n + 1]}")
    return found


def archived_apps(paths: list[str]) -> dict[str, str]:
    """Archived app names, minus any name that is live again under kubernetes/apps/."""
    archived = _app_dirs(paths, ARCHIVE_PREFIX)
    live = _app_dirs(paths, LIVE_PREFIX)
    return {name: where for name, where in archived.items() if name not in live}


def _tokens(path: str) -> set[str]:
    """Path components, each also without its extension(s)."""
    out: set[str] = set()
    for part in PurePosixPath(path).parts:
        if part in (".", ".."):
            continue
        out.add(part)
        out.add(part.split(".", 1)[0])
    return out


def _mentions(text: str, app: str) -> bool:
    """Whole-token mention: app names are [a-z0-9-], so a hyphen or word character extends one."""
    return re.search(rf"(?<![\w-]){re.escape(app)}(?![\w-])", text) is not None


def _walk_depends_on(node: object) -> list[str]:
    """Every dependsOn[].name anywhere in a parsed document."""
    names: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "dependsOn" and isinstance(value, list):
                names += [d["name"] for d in value if isinstance(d, dict) and isinstance(d.get("name"), str)]
            else:
                names += _walk_depends_on(value)
    elif isinstance(node, list):
        for item in node:
            names += _walk_depends_on(item)
    return names


def _kustomization_hits(path: str, doc: dict, apps: dict[str, str]) -> list[Hit]:
    hits: list[Hit] = []
    for field in ("resources", "components"):
        entries = doc.get(field)
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, str):
                hits += [
                    Hit(path, app, f"kustomization {field} entry '{entry}'")
                    for app in sorted(_tokens(entry) & apps.keys())
                ]
    return hits


def _document_hits(path: str, docs: list[object], apps: dict[str, str]) -> list[Hit]:
    hits: list[Hit] = []
    is_kustomization = PurePosixPath(path).name == "kustomization.yaml"
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if is_kustomization:
            hits += _kustomization_hits(path, doc, apps)
        for name in _walk_depends_on(doc):
            if name in apps:
                hits.append(Hit(path, name, f"dependsOn name '{name}'"))
        meta = doc.get("metadata")
        if isinstance(meta, dict) and meta.get("name") in apps:
            hits.append(Hit(path, meta["name"], f"{doc.get('kind', 'resource')} metadata.name '{meta['name']}'"))
    return hits


def find_hits(root: Path, paths: list[str], apps: dict[str, str]) -> list[Hit]:
    """Every reference under kubernetes/ to an app in apps, with the files read from root."""
    hits: list[Hit] = []
    for path in paths:
        if PurePosixPath(path).parts[:1] != ("kubernetes",):
            continue
        for app in sorted(_tokens(path) & apps.keys()):
            hits.append(Hit(path, app, f"path component '{app}'"))
        if not path.endswith((".yaml", ".yml")):
            continue
        text = (root / path).read_text(encoding="utf-8")
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError as err:
            named = sorted(a for a in apps if _mentions(text, a))
            hits += [Hit(path, a, f"unparsable YAML ({err.__class__.__name__}) naming the app") for a in named]
            continue
        hits += _document_hits(path, docs, apps)
    return hits


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "kubernetes", ".archive/kubernetes"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def run(root: Path, paths: list[str], allowlist: dict[str, str]) -> tuple[int, str]:
    """Return (exit code, report) for the tree at root."""
    apps = archived_apps(paths)
    relive = sorted(set(_app_dirs(paths, ARCHIVE_PREFIX)) - set(apps))
    if not apps:
        return 1, "archived-app-refs: no archived app found under .archive/; the path layout changed"
    hits = find_hits(root, paths, apps)
    used = {h.key for h in hits if h.key in allowlist}
    bad = [h for h in hits if h.key not in allowlist]
    stale = sorted(set(allowlist) - used)
    lines: list[str] = []
    if bad:
        lines.append("Archived apps still referenced under kubernetes/ (see docs/docs/operations/retiring-an-app.md):")
        lines += [f"  {h.path}: {apps[h.app]} -- {h.reason}" for h in bad]
    if stale:
        lines.append("ALLOWLIST entries that match nothing (remove them):")
        lines += [f"  {k}" for k in stale]
    if lines:
        return 1, "\n".join(lines)
    scanned = sum(1 for p in paths if p.startswith("kubernetes/"))
    return 0, (
        f"archived-app-refs: {len(apps)} archived apps, {scanned} files under kubernetes/, "
        f"no dangling reference ({len(used)} allowlisted; live again, so skipped: {', '.join(relive) or 'none'})"
    )


def main() -> int:
    """Check the repository the current directory is in."""
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )
    code, report = run(root, _tracked(root), ALLOWLIST)
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
