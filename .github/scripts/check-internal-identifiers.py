#!/usr/bin/env python3
"""Fail CI if a tracked file introduces an internal infrastructure identifier.

This repository is PUBLIC. The house rule (see CLAUDE.md / AGENTS.md) is: never
commit LAN IPs, node names, internal hostnames, MAC addresses, or vendor device
models that map the home network. This guard enforces that on every PR.

It scans all *tracked* files (so gitignored paths like docs/superpowers/ are never
seen) for the patterns below, skipping a small ALLOWLIST of files where a value is
an unavoidable, accepted functional config (Talos machine config, device cert
deployment, monitoring scrape targets, etc.). Anything matching OUTSIDE the
allowlist fails the build.

The allowlist is the authoritative list of "accepted functional configs" — each
entry says why the identifier has to live there. To template one out of git later,
move its value to the cluster-secrets 1Password item (Flux substitutes ${VAR} the
same way) and drop the allowlist entry.

Run locally:  python3 .github/scripts/check-internal-identifiers.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from fnmatch import fnmatch

# --- Patterns that must not appear in tracked files (outside the allowlist) ---
# NOTE: patterns are kept GENERIC on purpose — this script is public, so it must
# not itself enumerate device models or device-class names (that would re-disclose
# what it is meant to keep out of git). It catches the structural naming scheme
# (site-prefixed `cr-*` / `sw-*` hostnames, private IPs, MACs, internal TLDs).
PATTERNS: dict[str, re.Pattern] = {
    "LAN IP": re.compile(
        r"(?<![\d.])(?:10\.32|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?![\d.])"
    ),
    "node name": re.compile(r"cr-talos-\d+"),
    # site-prefixed device hostnames: cr-<word>-<word…> and sw-<word>-<word…>,
    # excluding the cluster nodes (cr-talos-*, caught above) and substrings of
    # longer tokens like "ghcr-auth" (the (?<![a-z0-9]) guard).
    "device hostname": re.compile(
        r"(?<![a-z0-9])cr-(?!talos(?:-|\b))[a-z][a-z0-9]*(?:-[a-z0-9]+)+"
        r"|(?<![a-z0-9])sw-(?:main|comms)-[a-z0-9]+"
    ),
    "MAC address": re.compile(
        r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:])"
    ),
    "internal hostname": re.compile(r"\b[a-z0-9_-]+\.(?:lan|internal)\b"),
}

# Values that match a pattern but are public-safe (cluster-internal CIDRs, k8s
# label keys, locally-administered placeholder MACs, the docs-placeholder token).
BENIGN = (
    re.compile(r"^10\.4[23]\."),          # Cilium pod/service CIDRs (10.42/10.43)
    re.compile(r"^grafana\.internal$"),   # k8s label key, not a hostname
    re.compile(r"^02:00:00:00:00"),       # locally-administered example MAC
)

# Accepted functional configs (glob -> reason). These are unavoidable values that
# operate the cluster; the topology they expose is an informed, documented
# acceptance (see .private/ inventory report).
ALLOWLIST: dict[str, str] = {
    "talos/**": "Talos machine config — node IPs/VIP/MACs/names are load-bearing",
    "kubernetes/apps/infrastructure/certwarden/cert-deployment/**":
        "device cert deployment — hostnames are k8s resource names / 1Password keys",
    ".github/workflows/image-pull.yaml": "CI passes node IPs to talosctl --nodes",
    "kubernetes/apps/default/shlink/app/helmrelease.yaml":
        "RFC1918 blocks in DISABLE_TRACKING_FROM (no specific subnet revealed)",
    "kubernetes/apps/home/homeassistant/app/helmrelease.yaml":
        "trusted-proxy IP + multus MAC for the home-automation pod",
    "kubernetes/apps/kube-system/cilium/app/networks.yaml": "LB IP pool / API host",
    "kubernetes/apps/kube-system/etcd-defrag/app/configmap.yaml":
        "etcd-defrag script targets nodes by IP/name",
    "kubernetes/apps/network/envoy-gateway/app/envoy.yaml":
        "Envoy gateway LoadBalancer listener IPs",
    "kubernetes/apps/network/scanopy/app/daemon-helmrelease.yaml": "LAN scan ranges",
    "kubernetes/apps/observability/blackbox-exporter/lan/probes.yaml":
        "blackbox LAN probe targets (hostname literal, domain templated)",
    "kubernetes/apps/observability/mktxp/app/externalsecret.yaml":
        "mktxp.conf section names = Prometheus instance labels",
    "kubernetes/apps/observability/network-ups-tools/app/configmap.yaml":
        "NUT UPS device names",
    "kubernetes/apps/observability/network-ups-tools/app/helmrelease.yaml":
        "NUT UPS device name + address",
    "kubernetes/apps/observability/nut-exporter/app/servicemonitor.yaml":
        "NUT exporter scrape targets",
    "kubernetes/apps/observability/snmp-exporter/app/configmap-entity-sensor.yaml":
        "SNMP sensor module references the core switch",
    "kubernetes/apps/observability/snmp-exporter/app/configmap.yaml":
        "SNMP module config",
    "kubernetes/apps/observability/snmp-exporter/app/helmrelease.yaml":
        "SNMP scrape target",
}


def allowlisted(path: str) -> bool:
    return any(fnmatch(path, glob) or path.startswith(glob.rstrip("*"))
               for glob in ALLOWLIST)


def tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return [f for f in out.splitlines() if not f.startswith(".private/")]


def main() -> int:
    violations: list[str] = []
    for path in tracked_files():
        if allowlisted(path) or path == ".github/scripts/check-internal-identifiers.py":
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for kind, pat in PATTERNS.items():
                for m in pat.finditer(line):
                    val = m.group(0)
                    if any(b.search(val) for b in BENIGN):
                        continue
                    violations.append(f"{path}:{lineno}: {kind}")
                    break

    if violations:
        print("Internal infrastructure identifiers found in tracked files:\n")
        for v in violations:
            print(f"  {v}")
        print(
            "\nThis repo is PUBLIC. Use placeholders (${SECRET_INTERNAL_DOMAIN},"
            " <node-ip>, …) or move the value to the cluster-secrets 1Password item."
            "\nIf the value is an unavoidable functional config, add its path to the"
            " ALLOWLIST in this script with a reason."
        )
        return 1

    print("OK — no new internal infrastructure identifiers in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
