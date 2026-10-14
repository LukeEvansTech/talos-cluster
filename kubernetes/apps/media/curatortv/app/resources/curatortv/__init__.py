"""Deterministic engine for the weekly Sonarr library cleanup routine.

A fork of the ``curator`` film engine with TV semantics: a series is the unit of
deletion, availability runs from episode imports, and viewing is counted in
distinct episodes per person. Judgement (is this show worth keeping?) belongs to
Claude; everything that must be *reliable* rather than tasteful lives here.

Entry point: ``python3 -m curatortv --help``.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
