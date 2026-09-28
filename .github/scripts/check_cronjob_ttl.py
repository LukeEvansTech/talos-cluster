#!/usr/bin/env python3
"""Fail if a CronJob in git does not reap its finished Jobs.

KubeJobFailed is `kube_job_failed > 0`, true for as long as a failed Job OBJECT
exists. Without ttlSecondsAfterFinished one transient failure pins the alert
until history rotation removes the Job: in September 2026 five CronJobs held it
for eleven days after every later run had succeeded, while known-noise.md
described the TTL as house style that 14 CronJobs did not actually have.

Checked shapes:
  - app-template HelmRelease: every controller with `type: cronjob` needs
    `cronjob.ttlSecondsAfterFinished` of at most 86400.
  - plain `kind: CronJob`: needs `spec.jobTemplate.spec.ttlSecondsAfterFinished` of at most 86400.

CronJobs a third-party chart renders itself (memini-fsck, volsync's kopia
maintenance) are not in git and so never seen here. ALLOWLIST is for a
git-tracked CronJob that must keep its Jobs; say why.

Run locally:  python3 .github/scripts/check_cronjob_ttl.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import yaml  # pylint: disable=import-error  # on the runner image; not in the linter's env

# "path::controller-or-name": reason
ALLOWLIST: dict[str, str] = {}
MAX_TTL = 86400

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


def _reaps_within_a_day(ttl: object) -> bool:
    """True for an integer TTL of at most a day; a longer one latches the alert for longer."""
    return isinstance(ttl, int) and not isinstance(ttl, bool) and 0 <= ttl <= MAX_TTL


def _problems_in(path: pathlib.Path) -> tuple[list[str], int]:
    """Return (problems, CronJobs examined) for one manifest file."""
    found: list[str] = []
    seen = 0
    text = path.read_text(encoding="utf-8")
    try:
        docs = list(yaml.safe_load_all(text))
    except yaml.YAMLError as err:
        # Only a problem if a CronJob could be hiding in it; an unrelated parse failure isn't ours.
        if "cronjob" in text.lower():
            return [f"{path}: unparsable YAML ({err.__class__.__name__}), cannot check its CronJob"], 0
        return [], 0
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind")
        spec = doc.get("spec") or {}
        if kind == "HelmRelease":
            controllers = ((spec.get("values") or {}).get("controllers")) or {}
            for name, ctl in controllers.items():
                if not isinstance(ctl, dict) or ctl.get("type") != "cronjob":
                    continue
                seen += 1
                if not _reaps_within_a_day((ctl.get("cronjob") or {}).get("ttlSecondsAfterFinished")):
                    key = f"{path}::{name}"
                    if key not in ALLOWLIST:
                        found.append(
                            f"{path}: controller '{name}' is type cronjob without "
                            "cronjob.ttlSecondsAfterFinished <= 86400"
                        )
        elif kind == "CronJob":
            seen += 1
            name = (doc.get("metadata") or {}).get("name", "?")
            job_spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
            if not _reaps_within_a_day(job_spec.get("ttlSecondsAfterFinished")):
                key = f"{path}::{name}"
                if key not in ALLOWLIST:
                    found.append(
                        f"{path}: CronJob '{name}' has no spec.jobTemplate.spec.ttlSecondsAfterFinished <= 86400"
                    )
    return found, seen


def main() -> int:
    """Check every tracked manifest; print a count on a pass so it is provably not empty."""
    files = _tracked_manifests()
    problems: list[str] = []
    seen = 0
    for f in files:
        found, n = _problems_in(f)
        problems += found
        seen += n
    if problems:
        print("CronJobs that never reap their finished Jobs (KubeJobFailed then latches):")
        for p in problems:
            print(f"  {p}")
        return 1
    if seen == 0:
        print(f"cronjob-ttl: {len(files)} manifests read but no CronJob found; the parser is broken")
        return 1
    print(f"cronjob-ttl: {seen} CronJobs in {len(files)} manifests, every one reaps its Jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
