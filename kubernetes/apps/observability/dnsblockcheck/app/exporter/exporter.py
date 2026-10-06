#!/usr/bin/env python3
"""Report external hosts the cluster depends on that NextDNS blocks.

The estate resolves through NextDNS, whose heuristic security features have
sinkholed real dependencies: code.forgejo.org ("untrusted-certs", Sep 2026)
and helm.coder.com ("dns-data-exfiltration", Oct 2026). Flux only showed a
vague not-ready after a timeout. This checks every Flux source host, every
running image's registry and a fixed list of registry download hosts, and
names the host and NextDNS's reason, so the fix (an allowlist entry in
codelooks-com/terraform-nextdns) is obvious.

A host counts as blocked when NextDNS answers 0.0.0.0 or attaches extended DNS
error 17 for it, or for any name in its CNAME chain: NextDNS flattens a chain
for a plain query, so a blocked CNAME target only shows when asked directly.
The chain comes from Google's DNS-over-HTTPS, which does not filter.

Configuration is entirely by environment variable; see helmrelease.yaml.
"""

import http.client
import http.server
import ipaddress
import json
import os
import random
import socket
import socketserver
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROFILE = os.environ["NEXTDNS_PROFILE"]
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9186"))
INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "600"))
EXTRA_HOSTS = [h.strip() for h in os.environ.get("EXTRA_HOSTS", "").split(",") if h.strip()]
# Reached by IP so the checker never depends on the resolver it is checking.
NEXTDNS_IP = os.environ.get("NEXTDNS_DOH_IP", "45.90.28.0")
GOOGLE_IP = os.environ.get("GOOGLE_DOH_IP", "8.8.8.8")
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"
FLUX_SOURCES = (
    "/apis/source.toolkit.fluxcd.io/v1/helmrepositories",
    "/apis/source.toolkit.fluxcd.io/v1/ocirepositories",
    "/apis/source.toolkit.fluxcd.io/v1/gitrepositories",
)

STATE = {"hosts": {}, "last_success": 0.0, "failures": 0}
LOCK = threading.Lock()


def tls_context(cafile=None):
    """A verifying TLS context that refuses anything older than TLS 1.2."""
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def log(msg):
    """Print a message to stdout, unbuffered so it reaches the pod log promptly."""
    print(msg, flush=True)


class ContinueExpired(Exception):
    """The API server returned 410 for a continue token; the list must restart."""


def _kube_page(url, token, ctx):
    """GET one page, retrying the 429s API priority-and-fairness returns to a busy service account."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 410 and "continue=" in url:
                raise ContinueExpired(url) from exc
            if exc.code != 429 or attempt == 5:
                raise RuntimeError(f"{url.removeprefix(API)}: HTTP {exc.code}") from exc
            time.sleep(int(exc.headers.get("Retry-After") or 1) + random.random() * 2)
    raise AssertionError("unreachable")


def kube_get(path):
    """List a Kubernetes API collection with the pod's service account, in pages."""
    with open(f"{SA_DIR}/token", encoding="utf-8") as f:
        token = f.read().strip()
    ctx = tls_context(cafile=f"{SA_DIR}/ca.crt")
    # Continue tokens expire, and a retried page can outlive one: restart the list from scratch.
    for _ in range(3):
        items, cont = [], ""
        try:
            while True:
                query = urllib.parse.urlencode({"limit": 200, **({"continue": cont} if cont else {})})
                page = _kube_page(f"{API}{path}?{query}", token, ctx)
                items += page.get("items", [])
                cont = page.get("metadata", {}).get("continue", "")
                if not cont:
                    return {"items": items}
        except ContinueExpired:
            log(f"{path}: continue token expired, restarting the list")
    raise RuntimeError(f"{path}: continue token expired three times")


def is_external(host):
    """Return True for a public DNS name, False for IPs and in-cluster names."""
    if not host or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return not host.endswith((".svc", ".cluster.local", ".local", ".internal", ".lan"))


def url_host(url):
    """Return the hostname of a Flux source URL (https, oci, ssh or scp-style git)."""
    if "://" not in url and "@" in url:
        url = "ssh://" + url.replace(":", "/", 1)
    return (urllib.parse.urlparse(url).hostname or "").lower()


def image_host(image):
    """Return the registry host an image reference pulls from."""
    first = image.split("/", 1)[0]
    if "/" not in image or ("." not in first and ":" not in first and first != "localhost"):
        return "registry-1.docker.io"
    host = first.split(":", 1)[0].lower()
    return "registry-1.docker.io" if host == "docker.io" else host


def discover(get=kube_get):
    """Map each external host the cluster depends on to the sources that need it."""
    found = {}
    for path in FLUX_SOURCES:
        for item in get(path).get("items", []):
            found.setdefault(url_host(item.get("spec", {}).get("url", "")), set()).add("flux")
    for pod in get("/api/v1/pods").get("items", []):
        spec = pod.get("spec", {})
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            found.setdefault(image_host(c.get("image", "")), set()).add("image")
    for host in EXTRA_HOSTS:
        found.setdefault(host.lower(), set()).add("extra")
    return {h: s for h, s in found.items() if is_external(h)}


def _name(data, off):
    """Skip a possibly compressed DNS name at off; return the offset after it."""
    while True:
        length = data[off]
        if length == 0:
            return off + 1
        if length & 0xC0 == 0xC0:
            return off + 2
        off += 1 + length


def nextdns_query(host):
    """Ask NextDNS for host's A record. Return (addresses, extended error texts)."""
    qid = random.randint(0, 0xFFFF)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.rstrip(".").split(".")) + b"\0"
    # One question (A, IN) and one OPT record advertising EDNS, so NextDNS attaches its reason.
    msg = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 1) + qname + struct.pack(">HH", 1, 1)
    msg += b"\0" + struct.pack(">HHIH", 41, 4096, 0, 0)
    ctx = tls_context()
    conn = http_conn(NEXTDNS_IP, "dns.nextdns.io", ctx)
    conn.request(
        "POST",
        f"/{PROFILE}",
        body=msg,
        headers={
            "Content-Type": "application/dns-message",
            "Accept": "application/dns-message",
        },
    )
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"NextDNS HTTP {resp.status}")
    _, _, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    off = 12
    for _ in range(qd):
        off = _name(data, off) + 4
    addrs, ext_errors = [], []
    for i in range(an + ns + ar):
        off = _name(data, off)
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[off : off + 10])
        off += 10
        rdata = data[off : off + rdlen]
        off += rdlen
        if i < an and rtype == 1 and rdlen == 4:
            addrs.append(socket.inet_ntoa(rdata))
        if rtype == 41:
            pos = 0
            while pos + 4 <= len(rdata):
                code, olen = struct.unpack(">HH", rdata[pos : pos + 4])
                if code == 15 and olen >= 2:
                    info = struct.unpack(">H", rdata[pos + 4 : pos + 6])[0]
                    text = rdata[pos + 6 : pos + 4 + olen].decode(errors="replace")
                    ext_errors.append((info, text))
                pos += 4 + olen
    return addrs, ext_errors


def http_conn(ip, sni, ctx):
    """An HTTPS connection to ip that presents and verifies the certificate for sni."""
    conn = http.client.HTTPSConnection(sni, 443, timeout=15, context=ctx)
    conn.sock = ctx.wrap_socket(socket.create_connection((ip, 443), timeout=15), server_hostname=sni)
    return conn


def cname_chain(host):
    """Return host plus every CNAME target Google's resolver follows from it."""
    conn = http_conn(GOOGLE_IP, "dns.google", tls_context())
    conn.request("GET", "/resolve?" + urllib.parse.urlencode({"name": host, "type": "A"}))
    body = json.load(conn.getresponse())
    conn.close()
    chain = [host]
    for answer in body.get("Answer", []):
        if answer.get("type") == 5:
            chain.append(answer["data"].rstrip(".").lower())
    return chain


def check(host):
    """Return (blocked reason or "", resolvable by the cluster's own DNS)."""
    reason = ""
    for name in cname_chain(host):
        addrs, ext_errors = nextdns_query(name)
        texts = [t for code, t in ext_errors if code == 17]
        if texts or "0.0.0.0" in addrs:
            reason = (texts[0].removeprefix("Blocked by NextDNS: ") if texts else "sinkholed") + (
                f" via {name}" if name != host else ""
            )
            break
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        resolvable = True
    except OSError:
        resolvable = False
    return reason, resolvable


def run_once():
    """Discover hosts, check each, and replace the published state.

    A host whose check fails keeps its previous result, so a known block cannot
    vanish from the metrics, and last_success only advances when every host was
    checked: a host that keeps failing then shows as a stale checker.
    """
    hosts = discover()
    with LOCK:
        previous = dict(STATE["hosts"])
    results, failed = {}, 0
    for host, sources in sorted(hosts.items()):
        entry = {"sources": ",".join(sorted(sources)), "failed": False}
        try:
            entry["reason"], entry["resolvable"] = check(host)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            failed += 1
            log(f"check {host} failed: {type(exc).__name__}: {exc}")
            prior = previous.get(host, {})
            entry.update(
                reason=prior.get("reason"),
                resolvable=prior.get("resolvable"),
                failed=True,
            )
        results[host] = entry
        if entry["reason"] or entry["resolvable"] is False:
            log(f"{host}: blocked={entry['reason'] or '-'} resolvable={entry['resolvable']}")
    with LOCK:
        STATE["hosts"] = results
        if not failed:
            STATE["last_success"] = time.time()
    log(f"checked {len(hosts) - failed}/{len(hosts)} hosts")


def loop():
    """Run forever. Never raise: a dead loop would look like nothing is blocked."""
    while True:
        try:
            run_once()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            with LOCK:
                STATE["failures"] += 1
            log(f"run failed: {type(exc).__name__}: {exc}")
        time.sleep(INTERVAL)


def _esc(value):
    """Escape a Prometheus label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render():
    """Render the current state as a Prometheus text-format exposition."""
    with LOCK:
        hosts = dict(STATE["hosts"])
        last, failures = STATE["last_success"], STATE["failures"]
    lines = [
        "# HELP dnsblockcheck_host_blocked 1 when NextDNS blocks the host or a name in its CNAME chain.",
        "# TYPE dnsblockcheck_host_blocked gauge",
    ]
    # A host whose first check failed has no result yet; it shows only in host_check_failed.
    known = {h: r for h, r in hosts.items() if r["reason"] is not None}
    for host, r in known.items():
        labels = f'host="{_esc(host)}",source="{r["sources"]}",reason="{_esc(r["reason"])}"'
        lines.append(f"dnsblockcheck_host_blocked{{{labels}}} {1 if r['reason'] else 0}")
    lines += [
        "# HELP dnsblockcheck_host_unresolvable 1 when the cluster's own DNS cannot resolve the host.",
        "# TYPE dnsblockcheck_host_unresolvable gauge",
    ]
    for host, r in known.items():
        lines.append(
            f'dnsblockcheck_host_unresolvable{{host="{_esc(host)}",source="{r["sources"]}"}} '
            f"{0 if r['resolvable'] else 1}"
        )
    lines += [
        "# HELP dnsblockcheck_host_check_failed 1 when the last check of the host errored.",
        "# TYPE dnsblockcheck_host_check_failed gauge",
    ]
    for host, r in hosts.items():
        lines.append(
            f'dnsblockcheck_host_check_failed{{host="{_esc(host)}",source="{r["sources"]}"}} '
            f"{1 if r['failed'] else 0}"
        )
    lines += [
        "# HELP dnsblockcheck_hosts Hosts checked in the last run.",
        "# TYPE dnsblockcheck_hosts gauge",
        f"dnsblockcheck_hosts {len(hosts)}",
        "# HELP dnsblockcheck_last_success_timestamp_seconds Last run that checked every host without error.",
        "# TYPE dnsblockcheck_last_success_timestamp_seconds gauge",
        f"dnsblockcheck_last_success_timestamp_seconds {last}",
        "# HELP dnsblockcheck_run_failures_total Runs that failed outright.",
        "# TYPE dnsblockcheck_run_failures_total counter",
        f"dnsblockcheck_run_failures_total {failures}",
    ]
    return ("\n".join(lines) + "\n").encode()


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve the exposition on /metrics and nothing else."""

    def do_GET(
        self,
    ):  # pylint: disable=invalid-name  # name is fixed by BaseHTTPRequestHandler
        """Handle a metrics scrape."""
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence per-request logging; a 60s scrape would otherwise dominate the pod log."""


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server so a slow scrape cannot block the next."""

    daemon_threads = True


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    Server(("", METRICS_PORT), Handler).serve_forever()
