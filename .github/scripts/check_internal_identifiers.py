#!/usr/bin/env python3
"""Fail CI if a tracked file introduces an internal infrastructure identifier.

This repository is PUBLIC. The house rule (see CLAUDE.md / AGENTS.md) is: never
commit LAN IPs, node names, internal hostnames, MAC addresses, or vendor device
models that map the home network. This guard enforces that on every PR.

It scans *tracked* files (so gitignored paths like docs/superpowers/ are never
seen) for the patterns below, skipping a small ALLOWLIST of files where a value is
an unavoidable, accepted functional config (Talos machine config, device cert
deployment, monitoring scrape targets, etc.). Anything matching OUTSIDE the
allowlist fails the build.

By default every tracked file is scanned (push-to-main + nightly runs). With
--diff-base REV, only files changed between REV and HEAD are scanned: PR runs
pass --diff-base HEAD^1 so a PR is judged ONLY on the files it touches. (PR
events run on the merge ref; a full scan there re-scans all of main, so a
transient bad state on main used to fail every open PR, and with
rebaseWhen:conflicted the stale FAILURE check-run then blocked Renovate's
self-merge forever. Main's own state stays covered by the push and schedule
runs.)

The allowlist is the authoritative list of "accepted functional configs" -- each
entry says why the identifier has to live there. To template one out of git later,
move its value to the cluster-secrets 1Password item (Flux substitutes ${VAR} the
same way) and drop the allowlist entry.

With --text-file PATH (or '-' for stdin) it scans PROSE instead of tracked files:
a commit message or a PR body. Those are the same world-visible surface -- a
squash merge copies the PR body verbatim into the commit message on main -- but
nothing linted them, so a LAN IP and an AI session link reached history in #3803.
Prose has no file path, so the ALLOWLIST does not apply; the BENIGN list still
does, and the prose-only PROSE_PATTERNS are added.

Run locally:  python3 .github/scripts/check_internal_identifiers.py
              python3 .github/scripts/check_internal_identifiers.py --text-file -
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from fnmatch import fnmatch

SELF_PATH = ".github/scripts/check_internal_identifiers.py"

# --- Patterns that must not appear in tracked files (outside the allowlist) ---
# NOTE: patterns are kept GENERIC on purpose -- this script is public, so it must
# not itself enumerate device models or device-class names (that would re-disclose
# what it is meant to keep out of git). It catches the structural naming scheme
# (site-prefixed `cr-*` / `sw-*` hostnames, private IPs, MACs, internal TLDs).
PATTERNS: dict[str, re.Pattern] = {
    "LAN IP": re.compile(r"(?<![\d.])(?:10\.32|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?![\d.])"),
    # Tailscale hands out addresses from the CGNAT range 100.64.0.0/10
    # (100.64.x.x-100.127.x.x); a literal one maps a tailnet node.
    "tailnet IP (CGNAT)": re.compile(r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+(?![\d.])"),
    "node name": re.compile(r"cr-talos-\d+"),
    # site-prefixed device hostnames, kept as two single-line patterns so black and
    # ruff agree; both exclude cr-talos-* and "ghcr-auth"-style substrings.
    "device hostname (cr)": re.compile(r"(?<![a-z0-9])cr-(?!talos(?:-|\b))[a-z][a-z0-9]*(?:-[a-z0-9]+)+"),
    "device hostname (sw)": re.compile(r"(?<![a-z0-9])sw-(?:main|comms)-[a-z0-9]+"),
    "MAC address": re.compile(r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:])"),
    "internal hostname": re.compile(r"\b[a-z0-9_-]+\.(?:lan|internal)\b"),
}

# Prose-only patterns (--text-file). These are not identifiers a tracked file
# would ever carry, but they do belong in commit messages and PR bodies: a
# session link publicly attributes the work and points at a private session.
PROSE_PATTERNS: dict[str, re.Pattern] = {
    "AI session link / co-author trailer": re.compile(
        r"claude\.ai/code/session|co-authored-by:\s*claude|generated with \[claude",
        re.IGNORECASE,
    ),
}

# A `git commit --verbose` message file carries the whole staged diff below this
# marker; everything below it is diff content, not the author's prose.
SCISSORS = re.compile(r"^# *-+ >8 -+")

# Values that match a pattern but are public-safe (cluster-internal CIDRs, k8s
# label keys, locally-administered placeholder MACs).
BENIGN = (
    re.compile(r"^10\.4[23]\."),  # Cilium pod/service CIDRs (10.42/10.43)
    re.compile(r"^grafana\.internal$"),  # k8s label key, not a hostname
    re.compile(r"^02:00:00:00:00"),  # locally-administered example MAC
    # Guaranteed-dead top of the CGNAT range: the cluster-secrets CI placeholder
    # for SEEDBOX_TAILNET_ADDR (real value comes from the ExternalSecret).
    re.compile(r"^100\.127\.255\.254$"),
)

# Accepted functional configs (glob -> short reason). These are unavoidable values
# that operate the cluster; the topology they expose is an informed, documented
# acceptance (see the .private/ inventory report).
ALLOWLIST: dict[str, str] = {
    "talos/**": "Talos machine config",
    "kubernetes/apps/infrastructure/certwarden/cert-deployment/**": "device cert deployment",
    ".github/workflows/image-pull.yaml": "talosctl --nodes in CI",
    "kubernetes/apps/default/shlink/app/helmrelease.yaml": "RFC1918 blocks (DISABLE_TRACKING_FROM)",
    "kubernetes/apps/home/homeassistant/app/helmrelease.yaml": "trusted-proxy IP + multus MAC",
    "kubernetes/apps/home/matter-server/app/helmrelease.yaml": "multus static IP + MAC",
    "kubernetes/apps/home/mosquitto/app/helmrelease.yaml": "multus static IP + MAC",
    "kubernetes/apps/home/scrypted/app/helmrelease.yaml": "multus static IP + MAC",
    "kubernetes/apps/home/zigbee2mqtt/app/helmrelease.yaml": "multus static IP + MAC",
    "kubernetes/apps/kube-system/cilium/app/networks.yaml": "LB IP pool / API host",
    "kubernetes/apps/kube-system/etcd-defrag/app/configmap.yaml": "etcd-defrag node targets",
    "kubernetes/apps/network/envoy-gateway/app/envoy.yaml": "Envoy LB listener IPs",
    "kubernetes/apps/network/scanopy/app/daemon-helmrelease.yaml": "LAN scan ranges",
    "kubernetes/apps/observability/blackbox-exporter/lan/probes.yaml": "blackbox LAN probe targets",
    "kubernetes/apps/observability/nut-exporter/app/prometheusrule.yaml": "NUT UPS alert selectors",
    "kubernetes/apps/observability/nut-exporter/app/servicemonitor.yaml": "NUT scrape targets",
    "kubernetes/apps/observability/snmp-exporter/app/configmap-entity-sensor.yaml": "SNMP sensor module",
    "kubernetes/apps/observability/snmp-exporter/app/configmap.yaml": "SNMP module config",
    "kubernetes/apps/observability/snmp-exporter/app/helmrelease.yaml": "SNMP scrape target",
}


def internal_domain_pattern() -> dict[str, re.Pattern]:
    """Return the optional internal-zone pattern, supplied out-of-band.

    PATTERNS above are deliberately generic because this script is PUBLIC --
    hardcoding the internal zone here would re-disclose exactly what the guard
    exists to keep out of git. So the regex arrives at runtime instead, via
    INTERNAL_DOMAIN_RE: a repository secret in CI, a gitignored .mise.local.toml
    locally. Unset (any fresh clone), the check simply skips -- every other
    pattern still applies.
    """
    raw = os.environ.get("INTERNAL_DOMAIN_RE", "").strip()
    if not raw:
        return {}
    try:
        return {"internal domain": re.compile(raw, re.IGNORECASE)}
    except re.error as exc:
        print(f"INTERNAL_DOMAIN_RE is not a valid regex: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def allowlisted(path: str) -> bool:
    """Return True if the path matches an accepted-functional-config glob."""
    return any(fnmatch(path, glob) or path.startswith(glob.rstrip("*")) for glob in ALLOWLIST)


def tracked_files() -> list[str]:
    """List git-tracked files, excluding the local-only .private/ tree."""
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return [f for f in out.splitlines() if not f.startswith(".private/")]


def changed_files(base: str) -> list[str]:
    """List tracked files changed between *base* and HEAD (deletions excluded)."""
    out = subprocess.check_output(["git", "diff", "--name-only", "--diff-filter=d", base, "HEAD"], text=True)
    tracked = set(tracked_files())
    return [f for f in out.splitlines() if f in tracked]


def scan_text(source: str, strip_git_comments: bool) -> int:
    """Scan a commit message or PR body and fail (exit 1) on any identifier."""
    if source == "-":
        raw = sys.stdin.read()
    else:
        with open(source, encoding="utf-8", errors="ignore") as handle:
            raw = handle.read()

    numbered = list(enumerate(raw.splitlines(), 1))
    if strip_git_comments:
        kept: list[tuple[int, str]] = []
        for lineno, line in numbered:
            if SCISSORS.match(line):
                break
            if not line.startswith("#"):
                kept.append((lineno, line))
        numbered = kept

    patterns = {**PATTERNS, **PROSE_PATTERNS, **internal_domain_pattern()}
    violations: list[str] = []
    for lineno, line in numbered:
        for kind, pat in patterns.items():
            for match in pat.finditer(line):
                if any(b.search(match.group(0)) for b in BENIGN):
                    continue
                violations.append(f"  line {lineno}: {kind}\n    {line.strip()[:120]}")
                break

    if violations:
        print("Internal identifiers found in commit message / PR body:\n")
        print("\n".join(violations))
        print(
            "\nThis repo is PUBLIC and a squash merge copies a PR body verbatim into"
            " the commit\nmessage on main, where it is permanent. Write the *why*"
            " without the coordinates:\nuse ${SECRET_DOMAIN} rather than the real name,"
            ' "the NAS endpoint" rather than its\naddress, and drop AI session links.'
            "\nDeliberate exception: git commit --no-verify"
        )
        return 1

    print("OK -- no internal identifiers in the supplied text.")
    return 0


def main() -> int:
    """Scan tracked files and fail (exit 1) on any non-allowlisted identifier."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diff-base",
        metavar="REV",
        help="scan only files changed between REV and HEAD (PR scope)",
    )
    parser.add_argument(
        "--text-file",
        metavar="PATH",
        help="scan prose (commit message / PR body) instead of tracked files; '-' reads stdin",
    )
    parser.add_argument(
        "--strip-git-comments",
        action="store_true",
        help="with --text-file, drop '#' comment lines and the verbose-diff block",
    )
    args = parser.parse_args()

    if args.text_file:
        return scan_text(args.text_file, args.strip_git_comments)

    paths = changed_files(args.diff_base) if args.diff_base else tracked_files()
    file_patterns = {**PATTERNS, **internal_domain_pattern()}
    violations: list[str] = []
    for path in paths:
        if allowlisted(path) or path == SELF_PATH:
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for kind, pat in file_patterns.items():
                for match in pat.finditer(line):
                    val = match.group(0)
                    if any(b.search(val) for b in BENIGN):
                        continue
                    violations.append(f"{path}:{lineno}: {kind}")
                    break

    if violations:
        print("Internal infrastructure identifiers found in tracked files:\n")
        for violation in violations:
            print(f"  {violation}")
        print(
            "\nThis repo is PUBLIC. Use placeholders (${SECRET_INTERNAL_DOMAIN},"
            " <node-ip>, ...) or move the value to the cluster-secrets 1Password"
            " item.\nIf the value is an unavoidable functional config, add its path"
            " to the ALLOWLIST in this script with a reason."
        )
        return 1

    scope = f"{len(paths)} changed" if args.diff_base else f"all {len(paths)} tracked"
    print(f"OK -- no new internal infrastructure identifiers ({scope} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
