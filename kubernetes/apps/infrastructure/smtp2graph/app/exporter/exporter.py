#!/usr/bin/env python3
"""Prometheus exporter and delivery canary for smtp2graph.

smtp2graph exposes no metrics of its own — it listens on :25 and nothing else,
and its config schema has no metrics or health options. This sidecar therefore
reads the relay's own on-disk queue directories and, separately, proves the
whole SMTP -> Graph path still works by sending a message through it.

It exists because between 2026-08-10 and 2026-08-19 every Graph send failed
with ErrorSendAsDenied — 353 messages lost, zero delivered — while the SMTP
listener stayed perfectly healthy. Liveness was never the problem; delivery
was. Everything here measures delivery outcome.

Configuration is entirely by environment variable; see helmrelease.yaml.
"""

import http.server
import os
import socketserver
import threading
import time
import uuid
from email.message import EmailMessage
from smtplib import SMTP

MAILROOT = os.environ.get("MAILROOT", "/data/mailroot")
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9184"))
SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
CANARY_ENABLED = os.environ.get("CANARY_ENABLED", "true").lower() == "true"
CANARY_INTERVAL = int(os.environ.get("CANARY_INTERVAL_SECONDS", "21600"))
CANARY_TIMEOUT = int(os.environ.get("CANARY_TIMEOUT_SECONDS", "180"))
CANARY_FROM = os.environ.get("CANARY_FROM", "")
CANARY_TO = os.environ.get("CANARY_TO", "")
CANARY_SETTLE = int(os.environ.get("CANARY_SETTLE_SECONDS", "5"))
STARTUP_DELAY = int(os.environ.get("CANARY_STARTUP_DELAY_SECONDS", "30"))

STATE = {"ok": 0, "last_success": 0.0, "last_attempt": 0.0, "runs": 0, "failures": 0}


def log(msg):
    """Print a message to stdout, unbuffered so it reaches the pod log promptly."""
    print(f"exporter: {msg}", flush=True)


def _entries(subdir):
    """Return the .eml filenames in a mailroot subdirectory, or [] if unreadable."""
    try:
        return [n for n in os.listdir(os.path.join(MAILROOT, subdir)) if n.endswith(".eml")]
    except OSError:
        return []


def count(subdir):
    """Return how many messages sit in a mailroot subdirectory."""
    return len(_entries(subdir))


def oldest_age(subdir):
    """Return the age in seconds of the oldest message in a subdirectory, or 0.0 if empty."""
    base = os.path.join(MAILROOT, subdir)
    stamps = []
    for name in _entries(subdir):
        try:
            stamps.append(os.path.getmtime(os.path.join(base, name)))
        except OSError:
            continue
    return max(0.0, time.time() - min(stamps)) if stamps else 0.0


def has_marker(subdir, marker):
    """Return True if any message in a subdirectory contains the canary marker."""
    base = os.path.join(MAILROOT, subdir)
    needle = marker.encode()
    for name in _entries(subdir):
        try:
            with open(os.path.join(base, name), "rb") as handle:
                if needle in handle.read():
                    return True
        except OSError:
            continue
    return False


def _submit(marker):
    """Submit one canary message to the relay. Return True if it was accepted."""
    msg = EmailMessage()
    msg["From"] = CANARY_FROM
    msg["To"] = CANARY_TO
    msg["Subject"] = "[canary] smtp2graph delivery check"
    msg.set_content(
        "Automated smtp2graph delivery canary — safe to delete.\n"
        "It exists because a silent Graph-side failure went unnoticed for nine days\n"
        "in Aug 2026 while the SMTP listener stayed perfectly healthy.\n"
        f"marker={marker}\n"
    )
    try:
        client = SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        client.send_message(msg)
        client.quit()
        return True
    except OSError as exc:  # covers every socket/SMTP transport failure
        log(f"canary: submit failed: {exc}")
        return False


def canary_once():
    """Send one canary and record whether it was delivered rather than dropped.

    Success means the message left the queue without ever appearing in failed/,
    which is only true if Graph actually accepted it.
    """
    marker = "smtp2graph-canary-" + uuid.uuid4().hex
    STATE["last_attempt"] = time.time()
    STATE["runs"] += 1
    if not _submit(marker):
        STATE["ok"] = 0
        STATE["failures"] += 1
        return
    # smtp2graph writes the file to queue/ before returning 250, so a short
    # settle avoids reading the directory before the file exists.
    time.sleep(CANARY_SETTLE)
    deadline = time.time() + CANARY_TIMEOUT
    while time.time() < deadline:
        if has_marker("failed", marker):
            log("canary: message landed in failed/ — Graph rejected it")
            STATE["ok"] = 0
            STATE["failures"] += 1
            return
        if not has_marker("queue", marker) and not has_marker("temp", marker):
            STATE["ok"] = 1
            STATE["last_success"] = time.time()
            log("canary: delivered")
            return
        time.sleep(5)
    log("canary: still queued at timeout")
    STATE["ok"] = 0
    STATE["failures"] += 1


def canary_loop():
    """Run the canary forever. Never raise: a dead loop would look like healthy silence."""
    time.sleep(STARTUP_DELAY)
    while True:
        try:
            canary_once()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Deliberately broad. This thread is the only thing proving mail
            # works; if it dies the metric simply freezes, which reads as "no
            # recent success" rather than as an alert.
            log(f"canary: unexpected error: {exc}")
            STATE["ok"] = 0
            STATE["failures"] += 1
        time.sleep(CANARY_INTERVAL)


def render():
    """Render the current state as a Prometheus text-format exposition."""
    lines = []

    def gauge(name, helptext, value):
        lines.extend([f"# HELP {name} {helptext}", f"# TYPE {name} gauge", f"{name} {value}"])

    def counter(name, helptext, value):
        lines.extend([f"# HELP {name} {helptext}", f"# TYPE {name} counter", f"{name} {value}"])

    gauge("smtp2graph_up", "Exporter is running (absence means monitoring is blind).", 1)
    gauge(
        "smtp2graph_messages_failed",
        "Messages the relay gave up on. Any non-zero value is mail that will never arrive.",
        count("failed"),
    )
    gauge("smtp2graph_messages_queued", "Messages awaiting delivery to Graph.", count("queue"))
    gauge("smtp2graph_oldest_queued_seconds", "Age of the oldest queued message.", round(oldest_age("queue"), 1))
    gauge("smtp2graph_canary_enabled", "Whether the end-to-end canary is running.", int(CANARY_ENABLED))
    gauge("smtp2graph_canary_success", "Result of the most recent canary (1 ok, 0 failed).", STATE["ok"])
    gauge(
        "smtp2graph_canary_last_success_timestamp_seconds",
        "Unix time of the last successful canary.",
        round(STATE["last_success"]),
    )
    gauge(
        "smtp2graph_canary_last_attempt_timestamp_seconds",
        "Unix time of the last canary attempt.",
        round(STATE["last_attempt"]),
    )
    counter("smtp2graph_canary_runs_total", "Canary attempts since start.", STATE["runs"])
    counter("smtp2graph_canary_failures_total", "Canary failures since start.", STATE["failures"])
    return "\n".join(lines) + "\n"


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    """Serve the exposition on /metrics and nothing else."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # pylint: disable=invalid-name  # name is fixed by BaseHTTPRequestHandler
        """Handle a metrics scrape."""
        if self.path.split("?", 1)[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Silence per-request logging; a 60s scrape would otherwise dominate the pod log."""


class ThreadedHTTPServer(socketserver.ThreadingTCPServer):
    """Threaded server so a slow scrape cannot block the next one."""

    allow_reuse_address = True
    daemon_threads = True


def main():
    """Start the canary thread (if configured) and serve metrics forever."""
    if CANARY_ENABLED and CANARY_FROM and CANARY_TO:
        threading.Thread(target=canary_loop, daemon=True).start()
    else:
        log("canary: disabled or unconfigured")
    log(f"listening on :{METRICS_PORT}, mailroot={MAILROOT}")
    ThreadedHTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler).serve_forever()


if __name__ == "__main__":
    main()
