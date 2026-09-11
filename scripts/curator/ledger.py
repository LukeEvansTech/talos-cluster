"""Durable state for the routine: baseline, decisions, crosswalk, action log.

The checkout is rebuilt on every run and a transcript is not a record, so state
that must outlive a run lives in object storage. Two things have to be durable or
the routine is unsafe: what it intended to do before it did it, and what a person
has already decided.

Layout under the bucket::

    baseline/current.json          last validated run snapshot
    decisions/<movie_id>.json      standing spare/dismissal + the facts it rested on
    firstseen/<movie_id>.json      fallback availability when history has aged out
    crosswalk/plex.json            Plex rating key -> external IDs
    runs/<run_id>/plan.json        the whole assessed population for one run
    runs/<run_id>/intent/<id>.json written BEFORE a deletion
    runs/<run_id>/outcome/<id>.json written AFTER, including "uncertain"
    locks/execute.json             cheap overlap guard

In DRY RUN every key is prefixed ``dry-run/`` so a rehearsal can never be mistaken
for the real record, and outcome records are never written at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .s3 import S3Client, S3Error

LOCK_KEY = "locks/execute.json"
LOCK_TTL_MINUTES = 90


class Ledger:
    """Namespaced reader/writer for the routine's durable state."""

    def __init__(self, client: S3Client, run_id: str, dry_run: bool = True):
        self.client = client
        self.run_id = run_id
        self.dry_run = dry_run
        self.prefix = "dry-run/" if dry_run else ""

    # --- plumbing ---------------------------------------------------------

    def _key(self, key: str) -> str:
        """Namespace a key so a dry run cannot overwrite real state."""
        return f"{self.prefix}{key}"

    def _read(self, key: str) -> dict | None:
        """Read and decode one JSON object, or None when absent/corrupt."""
        raw = self.client.get(self._key(key))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _write(self, key: str, payload: dict) -> None:
        """Write one JSON object."""
        self.client.put(self._key(key), json.dumps(payload, indent=2, sort_keys=True).encode())

    # --- baseline ---------------------------------------------------------

    def read_baseline(self) -> dict | None:
        """The last snapshot that passed validation, or None on first run."""
        return self._read("baseline/current.json")

    def write_baseline(self, snapshot: dict[str, Any]) -> None:
        """Record a validated snapshot as the new yardstick."""
        self._write("baseline/current.json", {**snapshot, "recorded_by_run": self.run_id})

    # --- standing human / routine decisions -------------------------------

    def read_decisions(self) -> dict[int, dict]:
        """Every standing spare or dismissal, keyed by Radarr movie id."""
        decisions: dict[int, dict] = {}
        for key in self.client.list_keys(self._key("decisions/")):
            raw = self.client.get(key)
            if raw is None:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            movie_id = record.get("movie_id")
            if isinstance(movie_id, int):
                decisions[movie_id] = record
        return decisions

    def write_decision(self, record: dict) -> None:
        """Persist one spare/dismissal so next week does not re-judge it."""
        self._write(f"decisions/{record['movie_id']}.json", record)

    # --- availability fallback -------------------------------------------

    def read_first_seen(self) -> dict[int, datetime]:
        """Persisted first-observed-playable timestamps."""
        record = self._read("firstseen/index.json") or {}
        out: dict[int, datetime] = {}
        for movie_id, stamp in record.items():
            try:
                out[int(movie_id)] = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
        return out

    def write_first_seen(self, index: dict[int, datetime]) -> None:
        """Persist first-observed timestamps.

        Write-once per film: an upgrade or rename must not push the clock
        forward, or a film sits permanently just inside its own window.
        """
        existing = self.read_first_seen()
        merged = {str(k): v.isoformat() for k, v in existing.items()}
        for movie_id, stamp in index.items():
            merged.setdefault(str(movie_id), stamp.isoformat())
        self._write("firstseen/index.json", merged)

    # --- Plex identity crosswalk -----------------------------------------

    def read_crosswalk(self) -> dict[int, dict]:
        """Cached rating-key -> external-id map, including retired keys."""
        raw = self._read("crosswalk/plex.json") or {}
        return {int(k): v for k, v in raw.items() if str(k).isdigit()}

    def write_crosswalk(self, crosswalk: dict[int, dict]) -> None:
        """Merge new mappings in, keeping historical keys.

        Plex reissues a rating key when an item is replaced; the old key is what
        old history rows carry, so dropping it loses those plays.
        """
        merged = {str(k): v for k, v in self.read_crosswalk().items()}
        merged.update({str(k): v for k, v in crosswalk.items()})
        self._write("crosswalk/plex.json", merged)

    # --- the action ledger ------------------------------------------------

    def record_plan(self, payload: dict) -> None:
        """Store the whole assessed population for this run."""
        self._write(f"runs/{self.run_id}/plan.json", payload)

    def record_intent(self, movie: dict, evidence: dict) -> None:
        """Write what is about to happen, BEFORE it happens."""
        self._write(
            f"runs/{self.run_id}/intent/{movie['movie_id']}.json",
            {
                "run_id": self.run_id,
                "movie": movie,
                "evidence": evidence,
                "intended_at": datetime.now(timezone.utc).isoformat(),
                "simulated": self.dry_run,
            },
        )

    def record_outcome(self, movie_id: int, status: str, detail: str, run_id: str | None = None) -> None:
        """Write what actually happened, including "uncertain".

        ``run_id`` targets another run's directory, which is how a reconciliation
        closes an intent left dangling by an earlier run. Written under the
        current run it would never match, so the intent would stay dangling and
        be re-counted against the deletion budget every week.

        A dry run records nothing here: completed-action records left by a
        rehearsal would make the next run think the work was done.
        """
        if self.dry_run:
            return
        target = run_id or self.run_id
        self._write(
            f"runs/{target}/outcome/{movie_id}.json",
            {
                "run_id": target,
                "movie_id": movie_id,
                "status": status,
                "detail": detail,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def deletions_in_window(self, days: int = 7) -> int:
        """Confirmed deletions recently recorded, across all runs.

        The per-run cap is only a real limit if a second run counts what the
        first already did.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        total = 0
        for key in self.client.list_keys(self._key("runs/")):
            if "/outcome/" not in key:
                continue
            raw = self.client.get(key)
            if raw is None:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("status") != "deleted":
                continue
            try:
                when = datetime.fromisoformat(str(record.get("recorded_at")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if when >= cutoff:
                total += 1
        return total

    def unreconciled_intents(self) -> list[dict]:
        """Intents from earlier runs with no matching outcome.

        Interrupted deletions -- a timeout, a killed run. Reconcile them against
        live state before anything new happens.
        """
        intents: dict[tuple[str, str], dict] = {}
        outcomes: set[tuple[str, str]] = set()
        for key in self.client.list_keys(self._key("runs/")):
            parts = key.split("/")
            if len(parts) < 4:
                continue
            run_id, kind, name = parts[-3], parts[-2], parts[-1]
            movie_id = name.removesuffix(".json")
            if kind == "outcome":
                outcomes.add((run_id, movie_id))
            elif kind == "intent":
                raw = self.client.get(key)
                if raw is None:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not record.get("simulated"):
                    intents[(run_id, movie_id)] = record
        return [record for ident, record in intents.items() if ident not in outcomes]

    # --- overlap guard ----------------------------------------------------

    def acquire_lock(self) -> tuple[bool, str]:
        """Best-effort guard against two runs acting at once.

        No compare-and-set here, so this is a read-then-write and a determined
        race can pass it. A guard, not a mutex -- what actually bounds concurrent
        damage is ``deletions_in_window``.
        """
        existing = self._read(LOCK_KEY)
        now = datetime.now(timezone.utc)
        if existing:
            try:
                expires = datetime.fromisoformat(str(existing.get("expires_at")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                expires = now - timedelta(seconds=1)
            if expires > now and existing.get("run_id") != self.run_id:
                return (
                    False,
                    f"run {existing.get('run_id')} holds the lock until {expires.isoformat()}",
                )
        self._write(
            LOCK_KEY,
            {
                "run_id": self.run_id,
                "acquired_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=LOCK_TTL_MINUTES)).isoformat(),
            },
        )
        return True, "acquired"

    def release_lock(self) -> None:
        """Drop the lock if this run still owns it."""
        existing = self._read(LOCK_KEY)
        if existing and existing.get("run_id") == self.run_id:
            try:
                self.client.delete(self._key(LOCK_KEY))
            except S3Error:
                pass
