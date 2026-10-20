#!/usr/bin/env python3
"""Fail if a LiteLLM virtual key is missing from the consumer tables in the docs.

Spend is estimated per key, from the "In-cluster consumers" section of
docs/docs/architecture/ai-llm-stack.md. A key added without a row there is
invisible to that estimate: the subtitles key (#6327) spent 75 USD of the
monthly credit in three days while the table predicted under 15 USD a month.

Every keyAlias of a tracked LiteLLMVirtualKey under kubernetes/ must appear in
backticks in that section.

Run locally:  python3 .github/scripts/check_virtualkey_docs.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DOC = Path("docs/docs/architecture/ai-llm-stack.md")
SECTION = "## In-cluster consumers"
ALIAS = re.compile(r"^\s+keyAlias:\s*['\"]?([\w.-]+)", re.MULTILINE)


def key_aliases() -> dict[str, str]:
    """Map each virtual key alias to the manifest that declares it."""
    files = subprocess.run(
        ["git", "grep", "-l", "-E", "^kind: LiteLLMVirtualKey$", "--", "kubernetes/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {alias: f for f in files for alias in ALIAS.findall(Path(f).read_text(encoding="utf-8"))}


def section_text() -> str:
    """The consumers section, from its heading up to the next level-two heading."""
    text = DOC.read_text(encoding="utf-8")
    start = text.index(SECTION)
    end = text.find("\n## ", start + len(SECTION))
    return text[start : end if end != -1 else None]


def main() -> int:
    """Report every key alias the section does not name."""
    section = section_text()
    missing = {a: f for a, f in key_aliases().items() if f"`{a}`" not in section}
    for alias, path in sorted(missing.items()):
        print(f"{path}: key `{alias}` has no row in {DOC} ({SECTION[3:]})", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
