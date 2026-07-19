#!/usr/bin/env python3
"""Fail if a ${SECRET_INTERNAL_DOMAIN} hostname has no ${SECRET_DOMAIN} sibling.

Deleting an internal-domain hostname is only safe when the same route also
publishes the primary domain. This guard makes that property checkable, so the
bulk removal cannot silently take an app offline (as it nearly did for memini).

Non-route uses of the variable are expected and allowlisted: device IPMI probe
targets, the NAS S3 endpoint, the external-dns domain filter, the wildcard cert
SAN, and the variable's own definition.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ALLOWLIST = {
    "kubernetes/components/global-vars/cluster-secrets.yaml": "defines the variable",
    "kubernetes/apps/observability/blackbox-exporter/lan/probes.yaml": "device IPMI probe targets",
    "kubernetes/apps/storage/garage/app/sync-cronjob.yaml": "NAS S3 endpoint, not a cluster route",
    "kubernetes/apps/network/opnsense-dns/app/helmrelease.yaml": "external-dns domainFilters",
    "kubernetes/apps/cert-manager/cert-manager/tls/certificate.yaml": "wildcard cert SAN",
}

# Anchored to a real YAML list item (`<indent>- "value"` or `<indent>- value`),
# not just any hyphen in the line -- an unanchored `-` also matches inside
# literal text like "prowler-api", which corrupts the captured hostname prefix
# and produces a false-positive orphan (see kubernetes/apps/security/prowler/
# app/helmrelease-api.yaml's comma-joined DJANGO_ALLOWED_HOSTS env value).
#
# The prefix capture excludes only the closing quote, not whitespace: most
# routes use the `{{ .Release.Name }}.${SECRET_INTERNAL_DOMAIN}` Helm template
# idiom, which contains spaces inside the quotes. Excluding `\s` too (as a
# naive first cut would) makes the regex blind to that idiom -- the majority
# of hostnames in this repo -- while still passing on unrelated files, which
# defeats the guard silently instead of loudly.
HOSTNAME_RE = re.compile(r'^\s*-\s*"?([^"]*)\$\{SECRET_INTERNAL_DOMAIN\}"?\s*$')


def sibling_re(prefix: str) -> re.Pattern:
    """Return a regex pattern matching a SECRET_DOMAIN hostname with the given prefix."""
    return re.compile(r'^\s*-\s*"?' + re.escape(prefix) + r'\$\{SECRET_DOMAIN\}"?\s*$')


def main() -> int:
    """Check that all internal-domain hostnames have a primary-domain sibling; exit 0 if OK, 1 if orphans found."""
    files = subprocess.check_output(["grep", "-rl", "SECRET_INTERNAL_DOMAIN", "kubernetes/"], text=True).split()
    orphans: list[str] = []
    for path in files:
        if path in ALLOWLIST:
            continue
        lines = pathlib.Path(path).read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            m = HOSTNAME_RE.search(line)
            if not m:
                continue
            pat = sibling_re(m.group(1))
            lo, hi = max(0, i - 4), min(len(lines), i + 5)
            if not any(pat.search(lines[j]) for j in range(lo, hi) if j != i):
                orphans.append(f"{path}:{i + 1}: {line.strip()}")

    if orphans:
        print("Internal-domain hostnames with no ${SECRET_DOMAIN} sibling:\n")
        for o in orphans:
            print(f"  {o}")
        print(
            "\nRemoving these would take the app offline. Switch them to "
            "${SECRET_DOMAIN} instead, or allowlist them in this script if they "
            "are not app routes."
        )
        return 1

    print("OK -- every internal-domain hostname has a primary-domain sibling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
