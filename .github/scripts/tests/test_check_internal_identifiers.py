"""Table tests for the internal-identifier patterns.

Every value here is synthetic. CAUGHT lists what each pattern must match; ALLOWED lists what
must pass, including requests to widen a pattern that review declined, each with its reason.
Answer a review finding about pattern coverage by adding a row here.

Run locally:  python3 -m unittest discover -s .github/scripts/tests
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "check_internal_identifiers.py"
SPEC = importlib.util.spec_from_file_location("check_internal_identifiers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

# (kind, text): the named pattern must flag the text.
CAUGHT = [
    ("LAN IP", "gateway 192.168.99.99"),
    ("LAN IP", "host 172.20.1.2"),
    ("LAN IP", "host 10.32.99.99"),
    ("tailnet IP (CGNAT)", "peer 100.100.1.1"),
    ("node name", "drain cr-talos-99"),
    ("device hostname (cr)", "ssh cr-example"),
    ("device hostname (sw)", "port on sw-main-example"),
    ("MAC address", "hwaddr aa:bb:cc:dd:ee:ff"),
    ("internal hostname", "http://nas.lan/"),
    ("internal hostname", "api.example.internal"),
    ("VLAN ID", "tagged VLAN 99"),
    ("VLAN ID", "VLAN ID: 99"),
    ("VLAN ID", 'vlanId: "99"'),
    ("VLAN ID", "vlan_id=99"),
    ("VLAN interface", '"master": "enp1s0np0.99"'),
    ("VLAN interface", "wan.99"),
    ("VLAN interface", "bond0.99"),
    ("storage device name", "array md9 degraded"),
    ("storage device name", "md127 resync"),
    ("storage device name", "/dev/sdz"),
    ("storage device name", "/dev/sdz1"),
    ("storage device name", "/dev/nvme9n1p2"),
    ("AI session link / co-author trailer", "https://claude.ai/code/session_example"),
    ("AI session link / co-author trailer", "Co-authored-by: Codex <bot@example.com>"),
    ("AI session link / co-author trailer", "Generated with Claude Code"),
    ("AI session link / co-author trailer", "noreply@anthropic.com"),
]

# (text, reason): no pattern may flag the text.
ALLOWED = [
    ("pod CIDR 10.42.0.0/16", "Cilium pod CIDR, listed in BENIGN"),
    ("app.kubernetes.io/grafana.internal", "k8s label key, listed in BENIGN"),
    ("mac 02:00:00:00:00:01", "locally administered example MAC"),
    ("ghcr-auth secret", "cr- inside a word is not a device hostname"),
    ("the md5 checksum", "md5 is a hash, not an array"),
    ("/dev/dm-1", "declined in #5677: kernel assigns dm-N at map time"),
    ("/dev/rbd4", "declined in #5677: kernel assigns rbdN at map time"),
    ("svc.namespace.svc.cluster.local", "in-cluster DNS is derivable from the tree"),
    ("<iot-vlan-id>", "the placeholder form the docs use"),
    ("version 1.2.3", "a dotted version is not an address"),
    ("host 10.33.1.1", "10.x outside 10.32 is not the LAN"),
    ("reviewed by Claude", "naming an assistant is not a trailer"),
]


def flagged(text: str) -> set[str]:
    """Return every pattern kind that flags text, applying BENIGN as scan_text does."""
    kinds = set()
    for kind, pat in {**guard.PATTERNS, **guard.PROSE_PATTERNS}.items():
        for match in pat.finditer(text):
            if not any(b.search(match.group(0)) for b in guard.BENIGN):
                kinds.add(kind)
    return kinds


class PatternTable(unittest.TestCase):
    """Check each table row against the live patterns."""

    def test_caught(self) -> None:
        """Each CAUGHT row is flagged by the pattern it names."""
        for kind, text in CAUGHT:
            with self.subTest(text=text):
                self.assertIn(kind, flagged(text))

    def test_allowed(self) -> None:
        """No pattern flags an ALLOWED row."""
        for text, reason in ALLOWED:
            with self.subTest(text=text, reason=reason):
                self.assertEqual(flagged(text), set())

    def test_every_pattern_has_a_caught_row(self) -> None:
        """A new pattern needs at least one CAUGHT row."""
        patterns = set(guard.PATTERNS) | set(guard.PROSE_PATTERNS)
        self.assertEqual(patterns - {kind for kind, _ in CAUGHT}, set())


if __name__ == "__main__":
    unittest.main()
