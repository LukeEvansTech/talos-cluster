"""Table tests for the internal-identifier patterns.

Every value here is synthetic. CAUGHT lists what each pattern must match; ALLOWED lists what
must pass, including requests to widen a pattern that review declined, each with its reason.
Answer a review finding about pattern coverage by adding a row here.

The AllowlistScoping tests below build their fixtures from CAUGHT rows rather than spelling out
new identifiers, so the guard's own allowlist entry for this file stays scoped to table rows.

Run locally:  python3 -m unittest discover -s .github/scripts/tests
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


def sample(kind: str) -> str:
    """Return the synthetic identifier the first CAUGHT row for *kind* contains."""
    text = next(t for k, t in CAUGHT if k == kind)
    match = guard.PATTERNS[kind].search(text)
    assert match is not None
    return match.group(0)


# CI sets GITHUB_ACTIONS, where an unset PRIVATE_NAMES_RE fails closed; give it a never-matching one.
NO_PRIVATE_NAMES = mock.patch.dict(os.environ, {"PRIVATE_NAMES_RE": "(?!x)x", "INTERNAL_DOMAIN_RE": ""})


class AllowlistScoping(unittest.TestCase):
    """An entry accepts one line's identifier, not the whole file, and a stale entry fails."""

    def setUp(self) -> None:
        NO_PRIVATE_NAMES.start()
        self.addCleanup(NO_PRIVATE_NAMES.stop)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(tmp)
        self.path = "app/config.yaml"
        Path("app").mkdir()
        ip, node = sample("LAN IP"), sample("node name")
        Path(self.path).write_text(f"router: {ip}\nbackup: {ip}\nnode: {node}\nrouter-name: {node}\n", encoding="utf-8")
        self.entry = guard.Allow(self.path, ("LAN IP",), "test router", contains="router: ")

    def scan(self, allowlist: tuple) -> tuple[list[str], set[int]]:
        """Scan the fixture file, returning violations and the indexes of entries used."""
        used: set[int] = set()
        return guard.scan_files([self.path], allowlist, used), used

    def test_line_scoped_entry_suppresses_only_its_line(self) -> None:
        """The same value on another line of the same file is still flagged."""
        violations, used = self.scan((self.entry,))
        self.assertNotIn(f"{self.path}:1: LAN IP", violations)
        self.assertIn(f"{self.path}:2: LAN IP", violations)
        self.assertEqual(used, {0})

    def test_other_identifier_in_same_file_still_flagged(self) -> None:
        """A different kind, even on a line the entry's text would match, is not accepted."""
        violations, _ = self.scan((self.entry,))
        self.assertIn(f"{self.path}:3: node name", violations)
        self.assertIn(f"{self.path}:4: node name", violations)

    def test_regex_entry(self) -> None:
        """A regex entry matches the line shape."""
        entry = guard.Allow(self.path, ("node name",), "test", regex=r"^node: \S+$")
        violations, used = self.scan((entry,))
        self.assertNotIn(f"{self.path}:3: node name", violations)
        self.assertIn(f"{self.path}:4: node name", violations)
        self.assertEqual(used, {0})

    def test_stale_entry_is_reported(self) -> None:
        """An entry that accepts nothing in a fully scanned file is stale."""
        stale_entry = guard.Allow(self.path, ("LAN IP",), "gone", contains="no such line")
        allowlist = (self.entry, stale_entry)
        _, used = self.scan(allowlist)
        stale = guard.stale_entries(allowlist, used, [self.path], [self.path], set(guard.PATTERNS))
        self.assertEqual(stale, [stale_entry])

    def test_entry_for_missing_file_is_stale(self) -> None:
        """An entry whose glob covers no tracked file is stale even on a partial scan."""
        gone = guard.Allow("app/deleted.yaml", ("LAN IP",), "deleted", contains="router: ")
        stale = guard.stale_entries((gone,), set(), [], [self.path], set(guard.PATTERNS))
        self.assertEqual(stale, [gone])

    def test_unscanned_file_is_not_judged(self) -> None:
        """A PR scan that skipped the entry's file cannot call the entry stale."""
        stale = guard.stale_entries((self.entry,), set(), [], [self.path], set(guard.PATTERNS))
        self.assertEqual(stale, [])

    def test_inactive_kind_is_not_judged(self) -> None:
        """An internal-domain entry is not stale where INTERNAL_DOMAIN_RE is unset."""
        entry = guard.Allow(self.path, ("internal domain",), "zone", contains="router: ")
        stale = guard.stale_entries((entry,), set(), [self.path], [self.path], set(guard.PATTERNS))
        self.assertEqual(stale, [])

    def test_stale_entry_fails_the_scan(self) -> None:
        """main() exits 1, names the entry and says to delete it."""
        stale_entry = guard.Allow(self.path, ("LAN IP",), "gone", contains="no such line")
        allowlist = (
            guard.Allow(self.path, ("LAN IP", "node name"), "all", regex=r"^\S+: "),
            stale_entry,
        )
        with (
            mock.patch.object(guard, "ALLOWLIST", allowlist),
            mock.patch.object(guard, "tracked_files", return_value=[self.path]),
            mock.patch("sys.argv", ["check"]),
            mock.patch("sys.stdout", new_callable=lambda: __import__("io").StringIO()) as out,
        ):
            code = guard.main()
        self.assertEqual(code, 1)
        self.assertIn(stale_entry.describe(), out.getvalue())
        self.assertIn("Delete each stale entry", out.getvalue())
        self.assertNotIn("found in tracked files", out.getvalue())

    def test_private_service_name_is_never_allowlisted(self) -> None:
        """An entry naming the private-service kind is malformed."""
        bad = guard.Allow(self.path, ("private service name",), "no", contains="x")
        self.assertTrue(guard.allowlist_errors((bad,)))

    def test_real_allowlist_is_well_formed(self) -> None:
        """Every shipped entry names known kinds, one matcher and a compiling regex."""
        self.assertEqual(guard.allowlist_errors(), [])


if __name__ == "__main__":
    unittest.main()
