#!/usr/bin/env python3
"""Reclaim redundant internal-domain host overrides from OPNsense Unbound.

WHY THIS EXISTS
---------------
`opnsense-dns` runs external-dns with `--policy=upsert-only --registry=noop`, so
external-dns NEVER deletes a record. Removing a hostname from Git stops new
records being created but never reclaims the old one. Reclaiming is therefore a
manual operation -- this script.

That matters because OPNsense's `searchHostOverride` endpoint truncates its
chunked response once the row count reaches ~421-424, at which point the webhook
gets malformed JSON and external-dns fails EVERY reconcile: no new DNS record can
be published cluster-wide. See the KB entry on the host-override ceiling.

THE TRAP THIS SCRIPT EXISTS TO AVOID
------------------------------------
An earlier ad-hoc version of this cleanup took the media stack offline. It built
its keep-list by grepping Git for `${SECRET_INTERNAL_DOMAIN}`, which structurally
cannot see an internal hostname stored as a *value* inside the `cluster-secrets`
Secret -- the repository only ever contains the variable name. It deleted the NFS
server's record (`SECRET_STORAGE_SERVER`), the NFS blackbox probe went red, and
the zeroscaler HPAs scaled every NFS-backed app to zero replicas.

So this script derives its keep-list from BOTH sources:
  1. Git manifests that reference `${SECRET_INTERNAL_DOMAIN}` directly.
  2. cluster-secrets / cluster-settings VALUES that contain an internal FQDN.

A candidate is deleted only if it is redundant (the same hostname already exists
on the primary domain) AND appears in neither source.

USAGE
-----
Runs in-cluster so it can read the Secret and reach the firewall:

    kubectl run reclaim-dns -n network --rm -i --restart=Never \\
      --image=python:3.12-slim --overrides='<see the KB entry>' -- \\
      python3 /scripts/reclaim_stale_dns.py

Environment:
  OPNSENSE_HOST / OPNSENSE_API_KEY / OPNSENSE_API_SECRET  (from opnsense-dns-secret)
  INTERNAL_DOMAIN / PRIMARY_DOMAIN                        (bare domains, no leading dot)
  KEEP_EXTRA      optional comma-separated extra hostnames to preserve
  APPLY=yes       actually delete; anything else is a dry run

It always prints a full JSON backup of every record (with UUIDs) before acting.
Capture that output -- it is the only way to restore a mistake.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request

HOST = os.environ["OPNSENSE_HOST"].rstrip("/")
INTERNAL = os.environ.get("INTERNAL_DOMAIN", "")
PRIMARY = os.environ.get("PRIMARY_DOMAIN", "")
APPLY = os.environ.get("APPLY") == "yes"

_auth = base64.b64encode(f"{os.environ['OPNSENSE_API_KEY']}:{os.environ['OPNSENSE_API_SECRET']}".encode()).decode()
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


class KeepListUnavailable(RuntimeError):
    """A keep-list source could not be read.

    This is fatal on purpose. If a source is unreachable the keep-list comes back
    short, every still-referenced hostname looks like a redundant alias, and the
    script would propose deleting exactly the records that must be preserved --
    which is the outage this tool exists to prevent. Fail closed, never open.
    """


def call(method: str, path: str) -> dict:
    """Call the OPNsense API. Content-Type is POST-only; a GET carrying it 400s."""
    headers = {"Authorization": f"Basic {_auth}"}
    data = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        data = b"{}"
    req = urllib.request.Request(f"{HOST}/api/unbound/{path}", method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, context=_ctx, timeout=90) as resp:
        return json.loads(resp.read() or b"{}")


def fetch_all() -> list[dict]:
    """Page through every host override. Never request all rows -- that is what truncates."""
    out: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"current": page, "rowCount": 200})
        batch = call("GET", f"settings/searchHostOverride?{query}")
        rows = batch.get("rows") or []
        if not rows:
            break
        out.extend(rows)
        if len(out) >= batch.get("total", 0):
            break
        page += 1
    return out


def hostnames_referenced_in_secrets() -> set[str]:
    """Internal-domain hostnames stored as VALUES in cluster-secrets / cluster-settings.

    This is the source the original cleanup missed. These never appear in Git --
    manifests only ever contain the variable name (e.g. ${SECRET_STORAGE_SERVER}).
    """
    found: set[str] = set()
    pattern = re.compile(rf"([a-z0-9][a-z0-9.-]*)\.{re.escape(INTERNAL)}\b")
    for kind, name in (("secret", "cluster-secrets"), ("configmap", "cluster-settings")):
        try:
            raw = subprocess.check_output(
                ["kubectl", "get", kind, name, "-n", "flux-system", "-o", "json"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise KeepListUnavailable(
                f"could not read {kind}/{name} in namespace flux-system ({type(exc).__name__}). "
                "Run where kubectl is available and authorised."
            ) from exc
        data = json.loads(raw).get("data", {}) or {}
        for value in data.values():
            text = str(value)
            if kind == "secret":
                try:
                    text = base64.b64decode(value).decode()
                except Exception:  # pylint: disable=broad-exception-caught  # non-utf8 values are not hostnames
                    continue
            found.update(m.group(1) for m in pattern.finditer(text))
    return found


def hostnames_referenced_in_git() -> set[str]:
    """Internal-domain hostnames written literally into tracked manifests."""
    try:
        raw = subprocess.check_output(
            ["git", "grep", "-hoE", r"[a-z0-9][a-z0-9.-]*\.\$\{SECRET_INTERNAL_DOMAIN\}", "--", "kubernetes/"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise KeepListUnavailable("git is not available. Run from inside a checkout of this repository.") from exc
    except subprocess.CalledProcessError as exc:
        # git grep exits 1 when there are no matches, which is a legitimate empty result.
        if exc.returncode != 1:
            raise KeepListUnavailable(f"git grep failed (exit {exc.returncode}).") from exc
        return set()
    return {line.split(".${")[0] for line in raw.split() if line}


def main() -> int:
    """Classify every internal-domain record and delete only the provably redundant ones."""
    if not INTERNAL or not PRIMARY:
        print("ABORT: set INTERNAL_DOMAIN and PRIMARY_DOMAIN")
        return 2

    rows = fetch_all()
    print(f"fetched {len(rows)} records")
    print("BACKUP_JSON_START")
    print(json.dumps(rows))
    print("BACKUP_JSON_END")

    try:
        keep = hostnames_referenced_in_secrets() | hostnames_referenced_in_git()
    except KeepListUnavailable as exc:
        print(f"\nABORT: {exc}")
        print("Refusing to continue with an incomplete keep-list -- that is how the NFS outage happened.")
        return 2
    keep |= {h.strip() for h in os.environ.get("KEEP_EXTRA", "").split(",") if h.strip()}
    print(f"\nkeep-list ({len(keep)} hostnames still referenced): {sorted(keep)}")

    primary_hosts = {r.get("hostname") for r in rows if r.get("domain") == PRIMARY}
    internal = [r for r in rows if r.get("domain") == INTERNAL]

    referenced = [r for r in internal if r.get("hostname") in keep]
    internal_only = [r for r in internal if r.get("hostname") not in primary_hosts and r.get("hostname") not in keep]
    targets = [r for r in internal if r.get("hostname") in primary_hosts and r.get("hostname") not in keep]

    print(f"\n{INTERNAL} records : {len(internal)}")
    print(f"  keep (referenced)  : {len(referenced)} -> {sorted(str(r.get('hostname') or '') for r in referenced)}")
    print(
        f"  keep (no twin)     : {len(internal_only)} -> {sorted(str(r.get('hostname') or '') for r in internal_only)}"
    )
    print(f"  DELETE (redundant) : {len(targets)} -> {sorted(str(r.get('hostname') or '') for r in targets)}")

    expected = os.environ.get("EXPECTED")
    if expected and len(targets) != int(expected):
        print(f"\nABORT: EXPECTED={expected} but computed {len(targets)}")
        return 1

    if not APPLY:
        print("\nDRY RUN -- set APPLY=yes to delete")
        return 0

    ok = fail = 0
    for record in targets:
        try:
            result = call("POST", f"settings/delHostOverride/{record['uuid']}")
            if result.get("result") in ("deleted", "ok"):
                ok += 1
            else:
                fail += 1
                print(f"  unexpected result for {record.get('hostname')}: {result}")
        except Exception as exc:  # pylint: disable=broad-exception-caught  # continue; partial success is recoverable
            fail += 1
            print(f"  FAILED {record.get('hostname')}: {exc}")

    print(f"\ndeleted={ok} failed={fail}")
    print("reconfigure:", call("POST", "service/reconfigure"))
    print(f"records now: {len(fetch_all())}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
