"""Configuration read from the environment.

Hosts and keys are supplied by the caller, never baked in: this repository is
public, and a hostname committed here maps the private network.
"""

from __future__ import annotations

import os
from datetime import datetime

from .model import utc


def env(name: str, required: bool = True) -> str:
    """Read a configuration value from the environment."""
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SystemExit(f"missing required environment variable {name}")
    return value


def env_int(name: str, default: int, minimum: int = 0) -> int:
    """An integer setting from the environment, or ``default`` when it is unset.

    A value that cannot be used stops the run: falling back would apply a limit
    nobody chose, in the one place the limits exist to be chosen.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, not {raw!r}") from None
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}, not {value}")
    return value


def scope_start() -> datetime | None:
    """The optional ``CLEANUP_SCOPE_START`` boundary; unset means the whole library."""
    raw = os.environ.get("CLEANUP_SCOPE_START", "").strip()
    if not raw:
        return None
    boundary = utc(raw)
    if boundary is None:
        raise SystemExit(f"CLEANUP_SCOPE_START must be an ISO date, not {raw!r}")
    return boundary


def tag_keeps_in_dry_run() -> bool:
    """Whether a dry run may still add the ``keep`` tag."""
    return os.environ.get("TAG_KEEPS_IN_DRY_RUN", "").strip().lower() == "true"


def adopt_rehearsal() -> bool:
    """Whether ``plan`` may carry the dry runs' record into an act ledger that has none."""
    return os.environ.get("CLEANUP_ADOPT_REHEARSAL", "").strip().lower() == "true"
