#!/usr/bin/env python3
"""Prometheus exporter for the Pushover account's monthly message quota.

Every app on the estate shares one Pushover account and its monthly cap. On
2026-09-28 the cap ran out and every alert went undelivered until the reset
on 2026-10-01, including the alert saying so. This reads the quota from
Pushover's limits endpoint, which does not itself use any quota, so the
cluster can warn by email before Pushover goes quiet.

Configuration is entirely by environment variable; see helmrelease.yaml.
"""

import http.server
import json
import os
import socketserver
import threading
import time
import urllib.parse
import urllib.request

TOKEN = os.environ["PUSHOVER_TOKEN"]
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9185"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
LIMITS_URL = "https://api.pushover.net/1/apps/limits.json"

STATE = {"limit": None, "remaining": None, "reset": None, "last_success": 0.0, "failures": 0}
LOCK = threading.Lock()


def poll_once():
    """Read the quota once and store it; raise on any failure."""
    # The token rides in the query string, so errors are logged without the URL.
    url = LIMITS_URL + "?" + urllib.parse.urlencode({"token": TOKEN})
    with urllib.request.urlopen(url, timeout=20) as resp:
        body = json.load(resp)
    if body.get("status") != 1:
        raise ValueError(f"status={body.get('status')} errors={body.get('errors')}")
    with LOCK:
        STATE["limit"] = int(body["limit"])
        STATE["remaining"] = int(body["remaining"])
        STATE["reset"] = int(body["reset"])
        STATE["last_success"] = time.time()


def poller():
    """Poll forever. Never raise: a dead loop would look like a healthy, unchanging quota."""
    while True:
        try:
            poll_once()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            with LOCK:
                STATE["failures"] += 1
            print(f"poll failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(POLL_INTERVAL)


def render():
    """Render the current state as a Prometheus text-format exposition."""
    with LOCK:
        s = dict(STATE)
    lines = [
        "# HELP pushover_poll_failures_total Failed reads of the Pushover limits endpoint.",
        "# TYPE pushover_poll_failures_total counter",
        f"pushover_poll_failures_total {s['failures']}",
        "# HELP pushover_last_success_timestamp_seconds Last successful read of the limits endpoint.",
        "# TYPE pushover_last_success_timestamp_seconds gauge",
        f"pushover_last_success_timestamp_seconds {s['last_success']}",
    ]
    if s["limit"] is not None:
        lines += [
            "# HELP pushover_message_limit Monthly message limit for the Pushover account.",
            "# TYPE pushover_message_limit gauge",
            f"pushover_message_limit {s['limit']}",
            "# HELP pushover_messages_remaining Messages left this month on the Pushover account.",
            "# TYPE pushover_messages_remaining gauge",
            f"pushover_messages_remaining {s['remaining']}",
            "# HELP pushover_limit_reset_timestamp_seconds When the monthly limit resets.",
            "# TYPE pushover_limit_reset_timestamp_seconds gauge",
            f"pushover_limit_reset_timestamp_seconds {s['reset']}",
        ]
    return ("\n".join(lines) + "\n").encode()


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve the exposition on /metrics and nothing else."""

    def do_GET(self):  # pylint: disable=invalid-name  # name is fixed by BaseHTTPRequestHandler
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
    threading.Thread(target=poller, daemon=True).start()
    Server(("", METRICS_PORT), Handler).serve_forever()
