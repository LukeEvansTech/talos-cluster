#!/usr/bin/env python3
"""Sync import list exclusions across all Radarr and Sonarr instances (additive only).

Pulls exclusions from all instances, combines them, and pushes missing entries
to each instance so they stay in sync. Removals are NOT synced.

API keys: mounted as files at /secrets/ (from ExternalSecret)
URLs: passed as environment variables
"""

import json
import os
import sys
import urllib.request
from datetime import datetime

SECRETS_DIR = "/secrets"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def read_secret(name):
    """Read a secret value from a mounted file."""
    path = os.path.join(SECRETS_DIR, name)
    with open(path) as f:
        return f.read().strip()


def api_get(url, key, endpoint):
    req = urllib.request.Request(
        f"{url}/{endpoint}",
        headers={"X-Api-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"  ERROR GET {url}/{endpoint}: {e}")
        return None


def api_post(url, key, endpoint, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{url}/{endpoint}",
        data=body,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        log(f"  ERROR POST {url}/{endpoint}: {e}")
        return 0, None


def sync_radarr():
    """Sync exclusions across Radarr instances by tmdbId."""
    instances = [
        ("radarr", os.environ["RADARR_URL"], read_secret("RADARR_KEY")),
        ("radarranime", os.environ["RADARRANIME_URL"], read_secret("RADARRANIME_KEY")),
    ]

    # Pull exclusions from all instances
    all_data = {}
    combined = {}

    for name, url, key in instances:
        data = api_get(url, key, "api/v3/exclusions")
        if data is None:
            log(f"  {name}: FAILED to fetch — skipping sync")
            return
        all_data[name] = data
        log(f"  {name}: {len(data)} exclusions")

        for item in data:
            tmdb = item["tmdbId"]
            if tmdb not in combined:
                combined[tmdb] = {
                    "tmdbId": tmdb,
                    "movieTitle": item.get("movieTitle", ""),
                    "movieYear": item.get("movieYear", 0),
                }

    log(f"  Combined: {len(combined)} unique movie exclusions")

    # Push missing to each instance
    for name, url, key in instances:
        existing = {item["tmdbId"] for item in all_data[name]}
        missing = [v for k, v in combined.items() if k not in existing]

        if not missing:
            log(f"  {name}: in sync")
            continue

        log(f"  {name}: adding {len(missing)} missing exclusions...")
        status, resp = api_post(url, key, "api/v3/exclusions/bulk", missing)

        if status == 200 and resp is not None:
            log(f"  {name}: added {len(resp)} exclusions")
        else:
            log(f"  {name}: bulk POST failed (HTTP {status})")


def sync_sonarr():
    """Sync exclusions across Sonarr instances by tvdbId."""
    instances = [
        ("sonarr", os.environ["SONARR_URL"], read_secret("SONARR_KEY")),
        ("sonarranime", os.environ["SONARRANIME_URL"], read_secret("SONARRANIME_KEY")),
    ]

    # Pull exclusions from all instances
    all_data = {}
    combined = {}

    for name, url, key in instances:
        data = api_get(url, key, "api/v3/importlistexclusion")
        if data is None:
            log(f"  {name}: FAILED to fetch — skipping sync")
            return
        all_data[name] = data
        log(f"  {name}: {len(data)} exclusions")

        for item in data:
            tvdb = item.get("tvdbId", 0)
            if tvdb and tvdb not in combined:
                combined[tvdb] = {
                    "tvdbId": tvdb,
                    "title": item.get("title", ""),
                }

    log(f"  Combined: {len(combined)} unique series exclusions")

    # Push missing to each instance (no bulk endpoint — individual POSTs)
    for name, url, key in instances:
        existing = {item.get("tvdbId", 0) for item in all_data[name]}
        missing = [v for k, v in combined.items() if k not in existing]

        if not missing:
            log(f"  {name}: in sync")
            continue

        log(f"  {name}: adding {len(missing)} missing exclusions...")
        added = 0
        failed = 0

        for item in missing:
            status, _ = api_post(url, key, "api/v3/importlistexclusion", item)
            if status == 201:
                added += 1
            else:
                failed += 1

        log(f"  {name}: added {added}, failed {failed}")


def main():
    log("=== Radarr exclusion sync ===")
    sync_radarr()
    log("=== Sonarr exclusion sync ===")
    sync_sonarr()
    log("=== Sync complete ===")


if __name__ == "__main__":
    main()
