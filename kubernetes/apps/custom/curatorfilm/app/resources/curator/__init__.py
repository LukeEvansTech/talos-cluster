"""Deterministic engine for the weekly Radarr library cleanup routine.

The routine's judgement (is this film worth keeping?) belongs to Claude. Everything
that must be *reliable* rather than tasteful lives here: provenance, availability,
viewing evidence, protection checks, batch limits and the action ledger.

The split matters because the two halves fail differently. A wrong judgement costs
one film; a wrong protection check costs a film that somebody explicitly protected,
silently, and the routine reports success either way.

Entry point: ``python3 -m curator --help`` (run from ``scripts/``).
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
