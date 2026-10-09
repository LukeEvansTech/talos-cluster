#!/usr/bin/env python3
"""Run chaski's route gates against sample payloads and fail on any wrong verdict.

A chaski `whenExpr` that errors at runtime evaluates to false, so a typo in a
gate drops every push without an error or an alert. In October 2026 two
backfills spent the shared Pushover quota and each fix was a new gate clause;
this pins what each gate must and must not send.

Fixtures live in .github/scripts/tests/chaski/<route>/ and are named
`fires-<case>.json` or `skips-<case>.json`. Every route with a `whenExpr`
needs at least one of each. `__DAYS_AGO_<N>__` in a fixture becomes an RFC 3339
timestamp N days before now, so date gates stay testable.

The config comes from the HelmRelease values and runs through `chaski validate`
in the image the chart pins (the OCIRepository tag), so a Renovate bump is
tested against the existing gates. Every `env "X"` the config reads is stubbed.

Run locally (needs docker):  python3 .github/scripts/check_chaski_routes.py
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml  # pylint: disable=import-error  # on the runner image; not in the linter's env

APP = pathlib.Path("kubernetes/apps/default/chaski/app")
FIXTURES = pathlib.Path(".github/scripts/tests/chaski")
IMAGE = "ghcr.io/home-operations/chaski"
DAYS_AGO = re.compile(r"__DAYS_AGO_(\d+)__")
FIRED = re.compile(r'"fired":\s*(true|false)')


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _expand(text: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

    def stamp(match: re.Match) -> str:
        when = now - datetime.timedelta(days=int(match.group(1)))
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")

    return DAYS_AGO.sub(stamp, text)


def _validate(image: str, work: pathlib.Path, env: list[str], extra: list[str]) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm", "-v", f"{work}:/w:ro"]
    for name in env:
        cmd += ["-e", f"{name}=stub"]
    cmd += [image, "validate", "--config", "/w/config.yaml", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def main() -> int:
    """Check fixture coverage, then each fixture's verdict; return 1 on any failure."""
    tag = _load(APP / "ocirepository.yaml")["spec"]["ref"]["tag"]
    image = f"{IMAGE}:{tag}"
    config = _load(APP / "helmrelease.yaml")["spec"]["values"]["config"]
    dumped = yaml.safe_dump(config, sort_keys=False)
    env = sorted(set(re.findall(r'env "([A-Z0-9_]+)"', dumped)))
    gated = sorted(name for name, route in config["routes"].items() if "whenExpr" in route)

    failures: list[str] = []
    for route in gated:
        names = [p.name for p in (FIXTURES / route).glob("*.json")]
        for kind in ("fires", "skips"):
            if not any(n.startswith(f"{kind}-") for n in names):
                failures.append(f"{route}: no {kind}-*.json fixture in {FIXTURES / route}")
    for stray in sorted(p.name for p in FIXTURES.iterdir() if p.is_dir() and p.name not in config["routes"]):
        failures.append(f"{FIXTURES / stray}: no route named {stray}")

    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        (work / "config.yaml").write_text(dumped, encoding="utf-8")
        res = _validate(image, work, env, [])
        if res.returncode != 0:
            failures.append(f"config: chaski validate exit {res.returncode}: {res.stderr.strip()[-400:]}")
        for fixture in sorted(FIXTURES.glob("*/*.json")):
            route, want = fixture.parent.name, fixture.name.startswith("fires-")
            if route not in config["routes"]:
                continue
            payload = _expand(fixture.read_text(encoding="utf-8"))
            json.loads(payload)
            (work / "payload.json").write_text(payload, encoding="utf-8")
            res = _validate(image, work, env, ["--route", route, "--payload", "/w/payload.json"])
            found = FIRED.search(res.stdout)
            if res.returncode != 0 or not found:
                failures.append(f"{fixture}: chaski validate exit {res.returncode}: {res.stderr.strip()[-400:]}")
                continue
            got = found.group(1) == "true"
            status = "ok" if got == want else "WRONG"
            print(f"{status:5} {route:16} {fixture.name}: fired={str(got).lower()}")
            if got != want:
                failures.append(f"{fixture}: expected fired={str(want).lower()}, got {str(got).lower()}")

    for line in failures:
        print(f"FAIL {line}", file=sys.stderr)
    print(f"{image}: {len(gated)} gated routes, {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
