#!/usr/bin/env python3
"""Fail CI if a tracked file introduces an internal infrastructure identifier.

This repository is PUBLIC. The house rule (see CLAUDE.md / AGENTS.md) is: never
commit LAN IPs, node names, internal hostnames, MAC addresses, or vendor device
models that map the home network. This guard enforces that on every PR.

It scans *tracked* files (so gitignored paths like docs/superpowers/ are never
seen) for the patterns below. An ALLOWLIST entry accepts one unavoidable functional
value (Talos machine config, device cert deployment, monitoring scrape targets,
etc.) by naming the file AND the line it sits on: a path glob, the identifier
kinds it accepts, and a literal substring or regex the line must contain. Any
other identifier in the same file still fails the build. Entries used to accept
whole files, which let an unrelated node IP hide in an allowlisted workflow for
months (#3151, removed in #6175).

An entry that no longer matches anything fails the scan too, so a stale entry
cannot linger and silently accept the next leak in its file.

By default every tracked file is scanned (push-to-main + nightly runs). With
--diff-base REV, only files changed between REV and HEAD are scanned: PR runs
pass --diff-base HEAD^1 so a PR is judged ONLY on the files it touches, unless
the PR edits this script, where the allowlist lives: then the whole tree is
scanned, since narrowing an entry can expose a value in a file the PR never
touched. Stale entries are judged only when every file an entry's glob covers
was scanned. (PR
events run on the merge ref; a full scan there re-scans all of main, so a
transient bad state on main used to fail every open PR, and with
rebaseWhen:conflicted the stale FAILURE check-run then blocked Renovate's
self-merge forever. Main's own state stays covered by the push and schedule
runs.)

The allowlist is the authoritative list of "accepted functional configs" -- each
entry says why the identifier has to live there. Match the line's shape or
context (its YAML key, a comment's fixed wording), never the value itself: this
script is public. To template one out of git later, move its value to the
cluster-secrets 1Password item (Flux substitutes ${VAR} the same way) and drop
the allowlist entry.

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
from dataclasses import dataclass
from fnmatch import fnmatch

SELF_PATH = ".github/scripts/check_internal_identifiers.py"

# Patterns stay generic on purpose: naming device models here would re-disclose what this
# public script hides. It matches structural shape instead (site-prefixed cr-*/sw-* hostnames,
# private IPs, MACs, internal TLDs).
PATTERNS: dict[str, re.Pattern] = {
    "LAN IP": re.compile(r"(?<![\d.])(?:10\.32|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?![\d.])"),
    # Tailscale CGNAT range 100.64.0.0/10 (100.64.x.x-100.127.x.x); a literal one maps a
    # tailnet node.
    "tailnet IP (CGNAT)": re.compile(r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+(?![\d.])"),
    "node name": re.compile(r"cr-talos-\d+"),
    # The leading lookbehind excludes "ghcr-auth"-style substrings. The trailing group is `*`,
    # not `+`: `+` required a second hyphenated segment, so a single-segment device name
    # (cr-<word>) once slipped past this guard and reached main.
    "device hostname (cr)": re.compile(r"(?<![a-z0-9])cr-(?!talos(?:-|\b))[a-z][a-z0-9]*(?:-[a-z0-9]+)*"),
    "device hostname (sw)": re.compile(r"(?<![a-z0-9])sw-(?:main|comms)-[a-z0-9]+"),
    "MAC address": re.compile(r"(?<![0-9a-fA-F:])(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}(?![0-9a-fA-F:])"),
    "internal hostname": re.compile(r"\b[a-z0-9_-]+\.(?:lan|internal)\b"),
    # Topology, not addresses: a VLAN ID or a host's storage device names map the network and
    # its hardware as surely as an IP does. Use a placeholder such as <iot-vlan-id>.
    "VLAN ID": re.compile(r"(?i)\bVLAN(?:[ _-]?ID)?[ :#=\"'-]*\d{1,4}\b|\bvlan_?id\s*[:=]\s*[\"']?\d{1,4}\b"),
    # A VLAN-tagged interface name (enp1s0np0.70, wan.70) carries the VLAN ID in its suffix.
    "VLAN interface": re.compile(r"\b(?:en[a-z0-9]+|eth\d+|bond\d+|wan)\.\d{1,4}\b"),
    # Any md array number (md0, md4, md127), but not an md5 hash reference.
    "storage device name": re.compile(r"\bmd(?!5\b)\d{1,3}\b|/dev/(?:sd[a-z]\d*|nvme\d+n\d+(?:p\d+)?)\b"),
}

# Prose-only: a tracked file would never carry these, but a commit message or PR body might,
# and a session link publicly attributes the work and points at a private session.
PROSE_PATTERNS: dict[str, re.Pattern] = {
    "AI session link / co-author trailer": re.compile(
        # The link bracket is optional: the plain "Generated with Claude Code" form (no link)
        # reached main several times before this matched it too. Matched by assistant NAME, not
        # e-mail domain, since addresses vary (Copilot gets its own alternative below).
        r"claude\.ai/code/session"
        r"|co-authored-by:\s*(?:claude|codex|copilot|chatgpt|openai|gemini|cursor|devin|aider)\b"
        r"|generated (?:with|by) \[?(?:claude|codex|copilot|chatgpt|gemini|cursor)"
        r"|noreply@anthropic\.com"
        r"|\d+\+copilot@users\.noreply\.github\.com",
        re.IGNORECASE,
    ),
}

# Below this marker in a `git commit --verbose` message is the staged diff, not the author's prose.
SCISSORS = re.compile(r"^# *-+ >8 -+")

# Values that match a pattern but are public-safe: cluster-internal CIDRs, k8s label keys,
# locally-administered placeholder MACs.
BENIGN = (
    re.compile(r"^10\.4[23]\."),  # Cilium pod/service CIDRs (10.42/10.43)
    re.compile(r"^grafana\.internal$"),  # k8s label key, not a hostname
    re.compile(r"^02:00:00:00:00"),  # locally-administered example MAC
    # Guaranteed-dead top of the CGNAT range: the CI placeholder for SEEDBOX_TAILNET_ADDR
    # (the real value comes from the ExternalSecret).
    re.compile(r"^100\.127\.255\.254$"),
)

WITHHELD = ("internal domain", "private service name")
# An account-disclosing service name is never an accepted functional config.
UNSUPPRESSIBLE = ("private service name",)


@dataclass(frozen=True)
class Allow:
    """One accepted identifier: a file glob, the kinds accepted, and the line that carries them.

    Exactly one of ``contains`` (a literal substring) or ``regex`` (searched with re.search)
    must be set and non-empty. ``where="path"`` matches it against the file path instead of a
    line, for an identifier in the file name itself. ``per_line`` caps how many identifiers of
    its kinds the entry accepts on one line, so a matched line cannot carry an extra one.
    """

    path: str
    kinds: tuple[str, ...]
    reason: str
    contains: str | None = None
    regex: str | None = None
    where: str = "line"
    per_line: int = 1

    def describe(self) -> str:
        """Render the entry for an error message (patterns hold no values, so they can be shown)."""
        how = f"contains {self.contains!r}" if self.contains is not None else f"regex {self.regex!r}"
        return f"{self.path} [{', '.join(self.kinds)}] {self.where} {how} -- {self.reason}"

    def accepts(self, path: str, kind: str, text: str, where: str) -> bool:
        """Return True if this entry accepts a *kind* identifier found in *text* of *path*."""
        if where != self.where or kind not in self.kinds or not fnmatch(path, self.path):
            return False
        if self.contains is not None:
            return self.contains in text
        return re.search(self.regex or "", text) is not None


ANY_PATTERN = tuple(PATTERNS)

# Accepted functional configs: unavoidable values that operate the cluster. The topology they
# expose is an informed, documented acceptance (see the .private/ inventory). Scope each entry
# to the line's shape or context and to the kinds it needs; one entry per distinct context.
ALLOWLIST: tuple[Allow, ...] = (
    # --- Talos machine config ---
    Allow(
        "talos/clusterconfig/.gitignore",
        ("node name",),
        "gitignored per-node kubeconfig names",
        regex=r"^kubernetes-\S+\.yaml$",
    ),
    Allow("talos/patches/controller/cluster.yaml", ("LAN IP",), "etcd advertisedSubnets", regex=r"^\s+- [\d.]+/24$"),
    Allow(
        "talos/patches/controller/cluster.yaml", ("LAN IP",), "API server certExtraSANs (VIP)", regex=r'^\s+- "[\d.]+"$'
    ),
    Allow(
        "talos/patches/controller/cluster.yaml",
        ("internal domain",),
        "API server certExtraSANs",
        regex=r'^\s+- "talos-cluster\.[^"]+"$',
    ),
    Allow("talos/patches/controller/machine-vip.yaml", ("LAN IP",), "Layer2 VIP address", regex=r"^name: [\d.]+$"),
    Allow(
        "talos/patches/global/machine-kubelet.yaml",
        ("LAN IP",),
        "kubelet nodeIP validSubnets",
        regex=r"^\s+- [\d.]+/24$",
    ),
    Allow(
        "talos/patches/global/machine-network.yaml", ("LAN IP",), "resolver nameserver", regex=r"^\s+- address: [\d.]+$"
    ),
    Allow(
        "talos/patches/global/machine-vlans.yaml",
        ("VLAN ID",),
        "VLANConfig header comment",
        regex=r"^# VLAN \d+, IoT-Trusted: ",
    ),
    Allow("talos/patches/global/machine-vlans.yaml", ("VLAN interface",), "VLANConfig name", regex=r"^name: wan\.\d+$"),
    Allow("talos/patches/global/machine-vlans.yaml", ("VLAN ID",), "VLANConfig vlanID", regex=r"^vlanID: \d+$"),
    Allow("talos/talconfig.yaml", ("LAN IP",), "cluster API endpoint (VIP)", regex=r"^endpoint: https://[\d.]+:6443$"),
    Allow("talos/talconfig.yaml", ("LAN IP",), "API/machine cert SANs (VIP)", regex=r'^\s+- "[\d.]+"$'),
    Allow(
        "talos/talconfig.yaml", ("internal domain",), "API/machine cert SANs", regex=r'^\s+- "talos-cluster\.[^"]+"$'
    ),
    Allow("talos/talconfig.yaml", ("node name",), "node hostnames", regex=r'^\s+- hostname: "[^"]+"$'),
    Allow("talos/talconfig.yaml", ("LAN IP",), "node addresses", regex=r'^\s+ipAddress: "[\d.]+"$'),
    Allow(
        "talos/talconfig.yaml",
        ("MAC address",),
        "uplink LinkAliasConfig selector",
        contains='match: mac(link.permanent_addr) == "',
    ),
    Allow("talos/talconfig.yaml", ("LAN IP",), "uplink LinkConfig address", regex=r"^\s+- address: [\d.]+/24$"),
    Allow("talos/talconfig.yaml", ("LAN IP",), "uplink LinkConfig default gateway", regex=r"^\s+- gateway: [\d.]+$"),
    # --- device cert deployment (certwarden) ---
    Allow(
        "kubernetes/apps/infrastructure/certwarden/cert-deployment/*/externalsecret*.yaml",
        ("device hostname (cr)",),
        "per-device ExternalSecret names and 1Password item keys",
        regex=r"^\s+(?:name|key): \S+$",
    ),
    Allow(
        "kubernetes/apps/infrastructure/certwarden/cert-deployment/brother/kustomization.yaml",
        ("device hostname (cr)",),
        "per-device ExternalSecret file list",
        regex=r"^\s+- externalsecret-\S+\.yaml$",
    ),
    Allow(
        "kubernetes/apps/infrastructure/certwarden/cert-deployment/brother/externalsecret-*.yaml",
        ("device hostname (cr)",),
        "per-device ExternalSecret file name",
        regex=r"/externalsecret-[a-z0-9-]+\.yaml$",
        where="path",
    ),
    # Not functional: a real device name used as the "e.g." in a usage comment. Kept narrow
    # pending a placeholder; do not widen.
    Allow(
        "kubernetes/apps/infrastructure/certwarden/cert-deployment/onyx/scripts-configmap.yaml",
        ("device hostname (cr)",),
        "usage comment example (real name; replace with a placeholder)",
        contains="#   ONYX_SWITCH - Custom env var: Onyx switch identifier (e.g., ",
    ),
    Allow(
        "kubernetes/apps/infrastructure/certwarden/cert-deployment/supermicro/scripts-configmap.yaml",
        ("device hostname (cr)",),
        "usage comment example (real name; replace with a placeholder)",
        contains="#   SUPERMICRO_HOST - Custom env var: Supermicro IPMI host identifier (e.g., ",
    ),
    # --- this guard's own test table ---
    Allow(
        ".github/scripts/tests/test_check_internal_identifiers.py",
        ANY_PATTERN,
        "synthetic pattern test fixtures (CAUGHT table rows)",
        regex=r'^\s+\("[^"]+", [\'"]',
    ),
    # --- app configs ---
    Allow(
        "kubernetes/apps/default/shlink/app/helmrelease.yaml",
        ("LAN IP",),
        "RFC1918 network blocks, not hosts",
        contains="DISABLE_TRACKING_FROM: ",
        per_line=2,
    ),
    Allow(
        "kubernetes/apps/home/homeassistant/app/helmrelease.yaml", ("LAN IP",), "multus static IP", contains='"ips": ["'
    ),
    Allow(
        "kubernetes/apps/home/matter-server/app/helmrelease.yaml", ("LAN IP",), "multus static IP", contains='"ips": ["'
    ),
    Allow("kubernetes/apps/home/scrypted/app/helmrelease.yaml", ("LAN IP",), "multus static IP", contains='"ips": ["'),
    Allow(
        "kubernetes/apps/home/zigbee2mqtt/app/helmrelease.yaml", ("LAN IP",), "multus static IP", contains='"ips": ["'
    ),
    Allow(
        "kubernetes/apps/kube-system/cilium/app/networks.yaml",
        ("LAN IP",),
        "LB IPAM pool",
        regex=r'^\s+- cidr: "[\d.]+/24"$',
    ),
    Allow(
        "kubernetes/apps/kube-system/etcd-defrag/app/configmap.yaml",
        ("LAN IP",),
        "etcd-defrag node targets",
        regex=r'^\s+NODE\d+="[\d.]+"$',
    ),
    Allow(
        "kubernetes/apps/kube-system/etcd-defrag/app/configmap.yaml",
        ("node name",),
        "etcd-defrag node target names",
        regex=r'^\s+NODE\d+_NAME="[^"]+"$',
    ),
    # Not functional: a real node address in a sample-output comment. Kept narrow pending a
    # placeholder; do not widen.
    Allow(
        "kubernetes/apps/kube-system/etcd-defrag/app/configmap.yaml",
        ("LAN IP",),
        "sample-output comment (real address; replace with a placeholder)",
        regex=r"^\s+# Example: [\d.]+ ",
    ),
    Allow(
        "kubernetes/apps/kube-system/multus/networks/iot.yaml",
        ("VLAN interface",),
        "multus VLAN master interface",
        regex=r'^\s+"master": "wan\.\d+",$',
    ),
    Allow(
        "kubernetes/apps/network/envoy-gateway/app/envoy.yaml",
        ("LAN IP",),
        "Envoy LB listener IPs",
        regex=r"^\s+lbipam\.cilium\.io/ips: [\d.]+$",
    ),
    Allow(
        "kubernetes/apps/observability/blackbox-exporter/lan/probes.yaml",
        ("device hostname (cr)",),
        "blackbox LAN probe targets",
        regex=r"^\s+- cr-[a-z0-9-]+\.\$\{SECRET_(?:INTERNAL_)?DOMAIN\}(?::443)?$",
    ),
    Allow(
        "kubernetes/apps/observability/bmc-exporter/app/externalsecret.yaml",
        ("device hostname (cr)",),
        "BMC Redfish host keys",
        regex=r"^\s+cr-[a-z0-9-]+\.\$\{SECRET_INTERNAL_DOMAIN\}:$",
    ),
    Allow(
        "kubernetes/apps/observability/bmc-exporter/app/externalsecret.yaml",
        ("device hostname (cr)",),
        "BMC 1Password item keys",
        regex=r"^\s+key: cr-[a-z0-9-]+$",
    ),
    # Not functional: names a device in a history comment. Kept narrow; do not widen.
    Allow(
        "kubernetes/apps/observability/bmc-exporter/app/externalsecret.yaml",
        ("device hostname (cr)",),
        "history comment (real name; reword without it)",
        contains="# The firewall's BMC, keyed by name since the ",
    ),
    Allow(
        "kubernetes/apps/observability/nut-exporter/app/prometheusrule.yaml",
        ("device hostname (cr)",),
        "NUT UPS alert selectors",
        contains="expr: absent(nut_ups_status{ups=",
        per_line=2,
    ),
    Allow(
        "kubernetes/apps/observability/nut-exporter/app/servicemonitor.yaml",
        ("device hostname (cr)",),
        "NUT scrape ups param",
        regex=r"^\s+- cr-[a-z0-9-]+$",
    ),
    Allow(
        "kubernetes/apps/observability/nut-exporter/app/servicemonitor.yaml",
        ("device hostname (cr)",),
        "NUT instance relabel",
        regex=r"^\s+replacement: cr-[a-z0-9-]+$",
    ),
)


def allowlist_errors(allowlist: tuple[Allow, ...] = ALLOWLIST) -> list[str]:
    """Return one message per malformed entry (unknown kind, bad matcher, uncompilable regex)."""
    known = set(PATTERNS) | {"internal domain"}
    errors: list[str] = []
    for entry in allowlist:
        if (entry.contains is None) == (entry.regex is None):
            errors.append(f"{entry.path}: set exactly one of contains= or regex=")
        elif not (entry.contains or entry.regex):
            # An empty matcher matches every line: a whole-file exception again.
            errors.append(f"{entry.path}: an empty matcher would accept every line")
        if entry.per_line < 1:
            errors.append(f"{entry.path}: per_line must be at least 1")
        if not entry.kinds:
            errors.append(f"{entry.path}: name the identifier kinds the entry accepts")
        for kind in entry.kinds:
            if kind in UNSUPPRESSIBLE:
                errors.append(f"{entry.path}: {kind!r} can never be allowlisted")
            elif kind not in known:
                errors.append(f"{entry.path}: unknown kind {kind!r}")
        if entry.where not in ("line", "path"):
            errors.append(f"{entry.path}: where= must be 'line' or 'path'")
        if entry.regex is not None:
            try:
                re.compile(entry.regex)
            except re.error as exc:
                errors.append(f"{entry.path}: bad regex {entry.regex!r}: {exc}")
    return errors


def private_names_pattern() -> dict[str, re.Pattern]:
    """Return the optional private-names pattern, supplied out-of-band like the internal zone.

    Naming a private tracker, a paid service or the VPN provider discloses an account; listing
    those names here would disclose them all at once. The regex arrives via PRIVATE_NAMES_RE,
    a repository secret in CI and a gitignored .mise.local.toml locally; unset, it is skipped.
    """
    raw = os.environ.get("PRIVATE_NAMES_RE", "").strip()
    if not raw:
        # Fail closed in CI: a missing secret would otherwise pass every account-naming leak.
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("PRIVATE_NAMES_RE is not set in CI; refusing to skip the check", file=sys.stderr)
            raise SystemExit(2)
        return {}
    try:
        return {"private service name": re.compile(raw, re.IGNORECASE)}
    except re.error as exc:
        print(f"PRIVATE_NAMES_RE is not a valid regex: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


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


def tracked_files() -> list[str]:
    """List git-tracked files, excluding the local-only .private/ tree."""
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return [f for f in out.splitlines() if not f.startswith(".private/")]


def changed_files(base: str) -> list[str]:
    """List tracked files changed between *base* and HEAD (deletions excluded)."""
    out = subprocess.check_output(["git", "diff", "--name-only", "--diff-filter=d", base, "HEAD"], text=True)
    tracked = set(tracked_files())
    return [f for f in out.splitlines() if f in tracked]


def format_violation(kind: str, lineno: int, line: str) -> str:
    """Render one prose violation, withholding the line for the internal domain.

    Echoing the offending line helps the author find it -- except for the
    internal domain, where a public repo's CI log would republish the very
    string the check exists to suppress. Secret masking does not help here: it
    masks the regex, not the domain that regex matched.
    """
    if kind in WITHHELD:
        return f"  line {lineno}: {kind} (content withheld -- it is the leak)"
    return f"  line {lineno}: {kind}\n    {line.strip()[:120]}"


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

    patterns = {**PATTERNS, **PROSE_PATTERNS, **internal_domain_pattern(), **private_names_pattern()}
    violations: list[str] = []
    for lineno, line in numbered:
        for kind, pat in patterns.items():
            for match in pat.finditer(line):
                if any(b.search(match.group(0)) for b in BENIGN):
                    continue
                violations.append(format_violation(kind, lineno, line))
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


def accepted(
    allowlist: tuple[Allow, ...],
    used: set[int],
    spent: dict[int, int],
    found: tuple[str, str, str, str],
) -> bool:
    """Return True if an entry with occurrences left accepts this identifier.

    *found* is (path, kind, text, where). *spent* counts, per entry, the identifiers already
    accepted on the current line; the first entry with room takes this one and records it.
    """
    path, kind, text, where = found
    for index, entry in enumerate(allowlist):
        if spent.get(index, 0) < entry.per_line and entry.accepts(path, kind, text, where):
            spent[index] = spent.get(index, 0) + 1
            used.add(index)
            return True
    return False


def scan_files(
    paths: list[str],
    allowlist: tuple[Allow, ...] = ALLOWLIST,
    used: set[int] | None = None,
) -> list[str]:
    """Return one 'path:line: kind' string per identifier no allowlist entry accepts.

    Indexes of the entries that accepted something are added to *used* for the stale check.
    """
    used = set() if used is None else used
    patterns = {**PATTERNS, **internal_domain_pattern(), **private_names_pattern()}
    # The guard's own source must still not name a private service.
    names_only = private_names_pattern()
    violations: list[str] = []
    for path in paths:
        active = names_only if path == SELF_PATH else patterns
        spent: dict[int, int] = {}
        for kind, pat in active.items():
            for match in pat.finditer(path):
                if any(b.search(match.group(0)) for b in BENIGN):
                    continue
                if kind not in UNSUPPRESSIBLE and accepted(allowlist, used, spent, (path, kind, path, "path")):
                    continue
                # A CI log is public: never echo a path that is itself the leak.
                shown = "<path withheld>" if kind in WITHHELD else path
                violations.append(f"{shown}: {kind} (in the file path)")
                break
        if not active:
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            text = line.rstrip("\r\n")
            spent = {}
            for kind, pat in active.items():
                for match in pat.finditer(text):
                    if any(b.search(match.group(0)) for b in BENIGN):
                        continue
                    if kind not in UNSUPPRESSIBLE and accepted(allowlist, used, spent, (path, kind, text, "line")):
                        continue
                    violations.append(f"{path}:{lineno}: {kind}")
                    break
    return violations


def stale_entries(
    allowlist: tuple[Allow, ...],
    used: set[int],
    scanned: list[str],
    tracked: list[str],
    active_kinds: set[str],
) -> list[Allow]:
    """Return entries that accepted nothing, where that is a fair verdict.

    An entry is judged only when every tracked file its glob covers was scanned (a PR scan sees
    a subset) and every kind it names had an active pattern (INTERNAL_DOMAIN_RE is unset in a
    fresh clone). An entry whose glob covers no tracked file at all is always stale.
    """
    scanned_set = set(scanned)
    stale: list[Allow] = []
    for index, entry in enumerate(allowlist):
        if index in used:
            continue
        covered = [f for f in tracked if fnmatch(f, entry.path)]
        if covered and not set(covered) <= scanned_set:
            continue
        if covered and not set(entry.kinds) <= active_kinds:
            continue
        stale.append(entry)
    return stale


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

    errors = allowlist_errors()
    if errors:
        print("Malformed ALLOWLIST entries:\n")
        print("\n".join(f"  {e}" for e in errors))
        return 2

    tracked = tracked_files()
    paths = changed_files(args.diff_base) if args.diff_base else tracked
    if args.diff_base and SELF_PATH in paths:
        # An allowlist edit is judged against the whole tree: narrowing an entry can expose a
        # value in a file this change never touched.
        print(f"{SELF_PATH} changed: scanning the whole tree.")
        paths = tracked
    used: set[int] = set()
    violations = scan_files(paths, ALLOWLIST, used)
    active_kinds = set(PATTERNS) | set(internal_domain_pattern())
    stale = stale_entries(ALLOWLIST, used, paths, tracked, active_kinds)

    if violations:
        print("Internal infrastructure identifiers found in tracked files:\n")
        for violation in violations:
            print(f"  {violation}")
        print(
            "\nThis repo is PUBLIC. Use placeholders (${SECRET_INTERNAL_DOMAIN},"
            " <node-ip>, ...) or move the value to the cluster-secrets 1Password"
            " item.\nIf the value is an unavoidable functional config, add an Allow entry"
            " to the ALLOWLIST in this script\nscoped to that line's shape or context"
            " (never the value itself), with a reason."
        )
    if stale:
        if violations:
            print()
        print("Stale ALLOWLIST entries (they no longer match anything):\n")
        for entry in stale:
            print(f"  {entry.describe()}")
        print(f"\nDelete each stale entry from ALLOWLIST in {SELF_PATH}.")
    if violations or stale:
        return 1

    scope = f"{len(paths)} changed" if paths is not tracked else f"all {len(paths)} tracked"
    print(f"OK -- no new internal infrastructure identifiers ({scope} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
