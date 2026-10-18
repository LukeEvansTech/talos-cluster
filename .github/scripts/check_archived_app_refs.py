#!/usr/bin/env python3
"""Fail if anything under kubernetes/ still references an archived app as a resource.

Archiving an app (AGENTS.md "Archiving an app") moves it to .archive/, but the
app usually has resources living in OTHER apps' directories: a LiteLLM virtual
key and its PushSecret, a dependsOn, a kustomization entry. Archiving
recommendarr in #6043 needed three such edits outside its own directory, and
nothing would have caught a missed one. The full runbook is
docs/docs/operations/retiring-an-app.md; this is its checkable step.

An archived app is a directory .archive/kubernetes/apps/<ns>/<app> holding a
ks.yaml. Its names are the directory name plus every Flux Kustomization that
ks.yaml declares, since a dependsOn names the Kustomization, which can differ.
Directories without a ks.yaml (partial archives such as retired MCP server
manifests of an app that is still live) are not apps and are skipped. A name a
live kubernetes/apps/<ns>/<app>/ks.yaml declares again is checked only as a
dependsOn on its old (namespace, name), since the name itself is live.

For each archived name, a tracked file under kubernetes/ fails when:
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
from dataclasses import field as dc_field
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
    where: str = dc_field(default="", compare=False)

    @property
    def key(self) -> str:
        """The ALLOWLIST key for this hit."""
        return f"{self.path}::{self.app}"


def _kustomization_names(text: str) -> list[str]:
    """metadata.name of every Flux Kustomization in a ks.yaml; none if it does not parse."""
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError:
        return []
    names = []
    for doc in docs:
        meta = doc.get("metadata") if isinstance(doc, dict) and doc.get("kind") == "Kustomization" else None
        if isinstance(meta, dict) and isinstance(meta.get("name"), str):
            names.append(meta["name"])
    return names


def _app_ids(root: Path, paths: list[str], prefix: tuple[str, ...]) -> dict[tuple[str, str], str]:
    """Map (ns, name) -> "<ns>/<app>" for every <prefix>/<ns>/<app>/ks.yaml: the directory name,
    plus each Kustomization it declares, which can differ (dragonfly declares dragonfly-cluster)."""
    found: dict[tuple[str, str], str] = {}
    n = len(prefix)
    for p in paths:
        parts = PurePosixPath(p).parts
        if len(parts) == n + 3 and parts[:n] == prefix and parts[-1] == "ks.yaml":
            where = f"{parts[n]}/{parts[n + 1]}"
            for name in [parts[n + 1], *_kustomization_names((root / p).read_text(encoding="utf-8"))]:
                found.setdefault((parts[n], name), where)
    return found


@dataclass(frozen=True)
class Archive:
    """What is archived: names no live app uses, and every archived (ns, name) identity."""

    names: dict[str, str]
    ids: dict[tuple[str, str], str]
    relive: list[str]


def archived_apps(root: Path, paths: list[str]) -> Archive:
    """Archived names, minus any a live ks.yaml declares again. The (ns, name) identities keep a
    revived name's OLD namespace checkable: archived ai/whisper is not live media/whisper."""
    archived = _app_ids(root, paths, ARCHIVE_PREFIX)
    live = _app_ids(root, paths, LIVE_PREFIX)
    live_names = {name for _, name in live}
    names = {name: where for (_, name), where in archived.items() if name not in live_names}
    ids = {k: v for k, v in archived.items() if k not in live}
    return Archive(names, ids, sorted({name for _, name in archived} & live_names))


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


def _walk_depends_on(node: object) -> list[tuple[str, str | None]]:
    """Every dependsOn entry anywhere in a parsed document, as (name, namespace or None)."""
    found: list[tuple[str, str | None]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "dependsOn" and isinstance(value, list):
                for d in value:
                    if isinstance(d, dict) and isinstance(d.get("name"), str):
                        ns = d.get("namespace")
                        found.append((d["name"], ns if isinstance(ns, str) else None))
            else:
                found += _walk_depends_on(value)
    elif isinstance(node, list):
        for item in node:
            found += _walk_depends_on(item)
    return found


def _own_namespace(path: str) -> str | None:
    """The namespace a dependsOn without one defaults to, from kubernetes/apps/<ns>/..."""
    parts = PurePosixPath(path).parts
    return parts[2] if len(parts) > 3 and parts[:2] == LIVE_PREFIX else None


def _depends_on_hits(path: str, doc: dict, archive: Archive) -> list[Hit]:
    hits: list[Hit] = []
    for name, ns in _walk_depends_on(doc):
        own = ns or _own_namespace(path)
        if name in archive.names:
            hits.append(Hit(path, name, f"dependsOn name '{name}'", archive.names[name]))
        elif own is not None and (own, name) in archive.ids:
            hits.append(Hit(path, name, f"dependsOn '{own}/{name}'", archive.ids[(own, name)]))
    return hits


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


def _document_hits(path: str, docs: list[object], archive: Archive) -> list[Hit]:
    apps = archive.names
    hits: list[Hit] = []
    is_kustomization = PurePosixPath(path).name == "kustomization.yaml"
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if is_kustomization:
            hits += _kustomization_hits(path, doc, apps)
        hits += _depends_on_hits(path, doc, archive)
        meta = doc.get("metadata")
        if isinstance(meta, dict) and meta.get("name") in apps:
            hits.append(Hit(path, meta["name"], f"{doc.get('kind', 'resource')} metadata.name '{meta['name']}'"))
    return hits


def find_hits(root: Path, paths: list[str], archive: Archive) -> list[Hit]:
    """Every reference under kubernetes/ to an archived app, with the files read from root."""
    apps = archive.names
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
        hits += _document_hits(path, docs, archive)
    return [h if h.where else Hit(h.path, h.app, h.reason, apps[h.app]) for h in hits]


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "kubernetes", ".archive/kubernetes"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    # A tracked file deleted but not yet staged (mid-step-2 of the runbook) is already gone.
    return sorted(p for p in out.split("\0") if p and (root / p).is_file())


def run(root: Path, paths: list[str], allowlist: dict[str, str]) -> tuple[int, str]:
    """Return (exit code, report) for the tree at root."""
    archive = archived_apps(root, paths)
    if not archive.ids:
        return 1, "archived-app-refs: no archived app found under .archive/; the path layout changed"
    hits = find_hits(root, paths, archive)
    used = {h.key for h in hits if h.key in allowlist}
    bad = [h for h in hits if h.key not in allowlist]
    stale = sorted(set(allowlist) - used)
    lines: list[str] = []
    if bad:
        lines.append("Archived apps still referenced under kubernetes/ (see docs/docs/operations/retiring-an-app.md):")
        lines += [f"  {h.path}: {h.where} -- {h.reason}" for h in bad]
    if stale:
        lines.append("ALLOWLIST entries that match nothing (remove them):")
        lines += [f"  {k}" for k in stale]
    if lines:
        return 1, "\n".join(lines)
    scanned = sum(1 for p in paths if p.startswith("kubernetes/"))
    return 0, (
        f"archived-app-refs: {len(archive.ids)} archived app and Kustomization names, {scanned} files under "
        f"kubernetes/, no dangling reference ({len(used)} allowlisted; live again, checked only by namespace: "
        f"{', '.join(archive.relive) or 'none'})"
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
