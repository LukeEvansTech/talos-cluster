#!/usr/bin/env python3
"""Validate every PrometheusRule with promtool and run the alert unit tests.

Two checks:
  - `promtool check rules` over the spec of every PrometheusRule in kubernetes/,
    which catches a PromQL syntax error before Flux applies it.
  - `promtool test rules` over each file in .github/scripts/tests/prometheus/.
    A test's rule_files name PrometheusRule manifests by repository path; they
    are rewritten to the extracted specs, so the test reads the real rule.

Both see the rules as Prometheus will after Flux postBuild: `$${VAR}` becomes
`${VAR}`, and a bare `${VAR}` (a cluster-secrets value) becomes a placeholder.

The alerts this exists for went wrong on series shape, not syntax: a counter
that appears at its first event, which increase() never counts, and a quiet
restart. A test should feed both shapes; see tests/prometheus/litellm.yaml.

Run locally:  mise x -- python3 .github/scripts/check_prometheus_rules.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml  # pylint: disable=import-error  # on the runner image; not in the linter's env

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / ".github/scripts/tests/prometheus"
SUBST = re.compile(r"(?<!\$)\$\{[A-Z0-9_]+\}")


def rule_manifests() -> list[str]:
    """Tracked files under kubernetes/ holding a PrometheusRule."""
    out = subprocess.run(
        ["git", "grep", "-l", "-E", "^kind: PrometheusRule$", "--", "kubernetes/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(out.split())


def extract(manifest: str, dest: Path) -> Path:
    """Write the manifest's rule groups as a plain Prometheus rule file, as Flux would render them."""
    text = (ROOT / manifest).read_text()
    text = SUBST.sub("FLUX_SUBSTITUTED", text).replace("$${", "${")
    docs = [d for d in yaml.safe_load_all(text) if d and d.get("kind") == "PrometheusRule"]
    out = dest / (manifest.replace("/", "__"))
    out.write_text(yaml.safe_dump({"groups": [g for d in docs for g in d["spec"]["groups"]]}))
    return out


def promtool_ok(*args: str) -> bool:
    """Run promtool, printing its output only on failure: a clean run lists 50+ files."""
    res = subprocess.run(["promtool", *args], capture_output=True, text=True, check=False)
    if res.returncode != 0:
        print(res.stdout + res.stderr, file=sys.stderr)
    return res.returncode == 0


def main() -> int:
    """Check every rule file, then run each test against the extracted rules."""
    if not shutil.which("promtool"):
        print("promtool not found; run via `mise x --` or install prometheus", file=sys.stderr)
        return 2
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        extracted = {m: extract(m, dest) for m in rule_manifests()}
        failed |= not promtool_ok("check", "rules", *map(str, extracted.values()))

        for test in sorted(TESTS.glob("*.yaml")):
            spec = yaml.safe_load(test.read_text())
            missing = [r for r in spec["rule_files"] if r not in extracted]
            if missing:
                print(f"{test.name}: rule_files not a PrometheusRule manifest: {missing}", file=sys.stderr)
                failed = True
                continue
            spec["rule_files"] = [str(extracted[r]) for r in spec["rule_files"]]
            rewritten = dest / test.name
            rewritten.write_text(yaml.safe_dump(spec))
            if not promtool_ok("test", "rules", str(rewritten)):
                print(f"failed: {test.relative_to(ROOT)}", file=sys.stderr)
                failed = True
    print(f"{len(extracted)} PrometheusRule manifests checked, {len(list(TESTS.glob('*.yaml')))} test files run")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
