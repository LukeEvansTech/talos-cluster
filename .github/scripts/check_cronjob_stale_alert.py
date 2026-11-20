#!/usr/bin/env python3
"""Fail if a CronJob in git has no alert on the age of its last success.

A CronJob that stops succeeding is silent by default: KubeJobFailed needs a failed
Job, so a suspended CronJob, one that never schedules, or one that never succeeds
after a rebuild raises nothing. In November 2026 13 of 30 CronJobs had no such
alert, the Postgres backups among them.

A CronJob passes when some PrometheusRule in git selects it by name, as
`cronjob="<name>"` or a `cronjob=~"<regex>"` that fully matches it, the shape
GickupBackupStale uses on kube_cronjob_status_last_successful_time. The rendered name of an app-template
controller depends on the chart, so the controller name, the release name and
`<release>-<controller>` are all accepted.

This does not catch a job that exits 0 without doing its work; only the job's own
exit codes can.

ALLOWLIST is for a CronJob that needs no such alert, or is watched another way;
say why.

Run locally:  python3 .github/scripts/check_cronjob_stale_alert.py
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import yaml  # pylint: disable=import-error  # on the runner image; not in the linter's env

NO_ALERT_YET = "predates this check; no last-success alert yet (hardening backlog H-24)"
# "path::controller-or-name": reason
ALLOWLIST: dict[str, str] = {
    "kubernetes/apps/ai/repowiki/app/helmrelease.yaml::repowiki-gen": NO_ALERT_YET,
    "kubernetes/apps/downloads/sabnzbd/app/cleanup-cronjob.yaml::sabnzbd-incomplete-cleanup": NO_ALERT_YET,
    "kubernetes/apps/downloads/sabnzbd/app/complete-cleanup-cronjob.yaml::sabnzbd-complete-cleanup": NO_ALERT_YET,
    "kubernetes/apps/infrastructure/nextdns/app/cronjob.yaml::nextdns-linkip": NO_ALERT_YET,
    "kubernetes/apps/kube-system/fstrim/app/helmrelease.yaml::fstrim": NO_ALERT_YET,
    "kubernetes/apps/media/exclusion-sync/app/cronjob.yaml::exclusion-sync": NO_ALERT_YET,
    "kubernetes/apps/media/imagemaid/app/helmrelease.yaml::imagemaid": NO_ALERT_YET,
    "kubernetes/apps/observability/glasshouse/app/helmrelease.yaml::wallpapers": NO_ALERT_YET,
    "kubernetes/apps/media/recyclarr/app/helmrelease.yaml::recyclarr": "disabled in the namespace kustomization",
}
SELECTED = re.compile(r'cronjob\s*=~?\s*"([^"]+)"')

# Tolerate the custom tags some manifests carry instead of refusing the whole file.
yaml.SafeLoader.add_multi_constructor("", lambda _loader, _suffix, _node: None)


def _tracked_manifests() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "ls-files", "kubernetes/*.yaml", "kubernetes/**/*.yaml"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return sorted({pathlib.Path(p) for p in out})


def _alerted(names: set[str], selectors: set[str]) -> bool:
    """True when a selector names, or as a regex fully matches, one of the rendered names."""
    for sel in selectors:
        try:
            if any(re.fullmatch(sel, n) for n in names):
                return True
        except re.error:
            continue
    return False


def _cronjobs_in(docs: list[object]) -> list[tuple[str, set[str]]]:
    """Return (allowlist name, acceptable rendered names) for each CronJob in one file."""
    found: list[tuple[str, set[str]]] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        name = (doc.get("metadata") or {}).get("name", "?")
        if doc.get("kind") == "CronJob":
            found.append((name, {name}))
        elif doc.get("kind") == "HelmRelease":
            controllers = (((doc.get("spec") or {}).get("values") or {}).get("controllers")) or {}
            for ctl_name, ctl in controllers.items():
                if isinstance(ctl, dict) and ctl.get("type") == "cronjob":
                    found.append((ctl_name, {ctl_name, name, f"{name}-{ctl_name}"}))
    return found


def main() -> int:
    """Check every tracked manifest; print a count on a pass so it is provably not empty."""
    files = _tracked_manifests()
    selected: set[str] = set()
    cronjobs: list[tuple[str, set[str]]] = []
    problems: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if "PrometheusRule" not in text and "cronjob" not in text.lower():
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError as err:
            problems.append(f"{path}: unparsable YAML ({err.__class__.__name__}), cannot check it")
            continue
        if any(isinstance(d, dict) and d.get("kind") == "PrometheusRule" for d in docs):
            selected.update(SELECTED.findall(text))
        cronjobs += [(f"{path}::{key}", names) for key, names in _cronjobs_in(docs)]
    seen = {key for key, _ in cronjobs}
    for key, names in cronjobs:
        if not _alerted(names, selected) and key not in ALLOWLIST:
            problems.append(f'{key}: no PrometheusRule selects cronjob="{sorted(names)[0]}"')
    for key in sorted(set(ALLOWLIST) - seen):
        problems.append(f"{key}: allowlisted but no such CronJob in git; drop the entry")
    for key, names in cronjobs:
        if key in ALLOWLIST and _alerted(names, selected):
            problems.append(f"{key}: allowlisted but an alert now selects it; drop the entry")
    if problems:
        print("CronJobs with no alert on the age of their last success:")
        for p in problems:
            print(f"  {p}")
        print("Add one shaped like GickupBackupStale, or an ALLOWLIST entry with the reason.")
        return 1
    if not cronjobs or not selected:
        print(f"cronjob-stale-alert: {len(files)} manifests read but nothing to compare; the parser is broken")
        return 1
    spared = len(ALLOWLIST)
    print(f"cronjob-stale-alert: {len(cronjobs)} CronJobs, {len(cronjobs) - spared} alerted, {spared} allowlisted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
