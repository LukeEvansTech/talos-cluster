import http.server, os, socketserver, threading, time, uuid, smtplib
from email.message import EmailMessage

MAILROOT = os.environ.get("MAILROOT", "/data/mailroot")
PORT = int(os.environ.get("METRICS_PORT", "9184"))
SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
CANARY_ENABLED = os.environ.get("CANARY_ENABLED", "true").lower() == "true"
CANARY_INTERVAL = int(os.environ.get("CANARY_INTERVAL_SECONDS", "21600"))
CANARY_TIMEOUT = int(os.environ.get("CANARY_TIMEOUT_SECONDS", "180"))
CANARY_FROM = os.environ.get("CANARY_FROM", "")
CANARY_TO = os.environ.get("CANARY_TO", "")
SETTLE = int(os.environ.get("CANARY_SETTLE_SECONDS", "5"))

S = {"ok": 0, "last_success": 0.0, "last_attempt": 0.0, "runs": 0, "failures": 0}

def _names(d):
    try:
        return [n for n in os.listdir(os.path.join(MAILROOT, d)) if n.endswith(".eml")]
    except OSError:
        return []

def count(d):
    return len(_names(d))

def oldest_age(d):
    p = os.path.join(MAILROOT, d)
    ts = []
    for n in _names(d):
        try:
            ts.append(os.path.getmtime(os.path.join(p, n)))
        except OSError:
            continue
    return max(0.0, time.time() - min(ts)) if ts else 0.0

def has_marker(d, marker):
    p = os.path.join(MAILROOT, d)
    needle = marker.encode()
    for n in _names(d):
        try:
            with open(os.path.join(p, n), "rb") as fh:
                if needle in fh.read():
                    return True
        except OSError:
            continue
    return False

def canary_once():
    marker = "smtp2graph-canary-" + uuid.uuid4().hex
    m = EmailMessage()
    m["From"] = CANARY_FROM
    m["To"] = CANARY_TO
    m["Subject"] = "[canary] smtp2graph delivery check"
    m.set_content(
        "Automated smtp2graph delivery canary — safe to delete.\n"
        "It exists because a silent Graph-side failure went unnoticed for 9 days\n"
        "in Aug 2026 while the SMTP listener stayed perfectly healthy.\n"
        f"marker={marker}\n")
    S["last_attempt"] = time.time()
    S["runs"] += 1
    try:
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        s.send_message(m)
        s.quit()
    except Exception as e:
        print(f"canary: submit failed: {e}", flush=True)
        S["ok"] = 0
        S["failures"] += 1
        return
    # smtp2graph writes the file to queue/ before returning 250, so a short
    # settle is enough to avoid reading before the file exists.
    time.sleep(SETTLE)
    deadline = time.time() + CANARY_TIMEOUT
    while time.time() < deadline:
        if has_marker("failed", marker):
            print("canary: message landed in failed/ — Graph rejected it", flush=True)
            S["ok"] = 0
            S["failures"] += 1
            return
        if not has_marker("queue", marker) and not has_marker("temp", marker):
            S["ok"] = 1
            S["last_success"] = time.time()
            print("canary: delivered", flush=True)
            return
        time.sleep(5)
    print("canary: still queued at timeout", flush=True)
    S["ok"] = 0
    S["failures"] += 1

def canary_loop():
    time.sleep(30)  # let the relay finish starting
    while True:
        try:
            canary_once()
        except Exception as e:
            print(f"canary: unexpected error: {e}", flush=True)
            S["ok"] = 0
        time.sleep(CANARY_INTERVAL)

def render():
    L = []
    def g(name, help_, val):
        L.append(f"# HELP {name} {help_}")
        L.append(f"# TYPE {name} gauge")
        L.append(f"{name} {val}")
    g("smtp2graph_up", "Exporter is running (absence means monitoring is blind).", 1)
    g("smtp2graph_messages_failed",
      "Messages the relay gave up on. Any non-zero value is mail that will never arrive.",
      count("failed"))
    g("smtp2graph_messages_queued", "Messages awaiting delivery to Graph.", count("queue"))
    g("smtp2graph_oldest_queued_seconds", "Age of the oldest queued message.",
      round(oldest_age("queue"), 1))
    g("smtp2graph_canary_enabled", "Whether the end-to-end canary is running.",
      1 if CANARY_ENABLED else 0)
    g("smtp2graph_canary_success", "Result of the most recent canary (1 ok, 0 failed).", S["ok"])
    g("smtp2graph_canary_last_success_timestamp_seconds",
      "Unix time of the last successful canary.", round(S["last_success"], 0))
    g("smtp2graph_canary_last_attempt_timestamp_seconds",
      "Unix time of the last canary attempt.", round(S["last_attempt"], 0))
    L.append("# HELP smtp2graph_canary_runs_total Canary attempts since start.")
    L.append("# TYPE smtp2graph_canary_runs_total counter")
    L.append(f"smtp2graph_canary_runs_total {S['runs']}")
    L.append("# HELP smtp2graph_canary_failures_total Canary failures since start.")
    L.append("# TYPE smtp2graph_canary_failures_total counter")
    L.append(f"smtp2graph_canary_failures_total {S['failures']}")
    return "\n".join(L) + "\n"

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

if __name__ == "__main__":
    if CANARY_ENABLED and CANARY_FROM and CANARY_TO:
        threading.Thread(target=canary_loop, daemon=True).start()
    else:
        print("canary: disabled or unconfigured", flush=True)
    print(f"exporter: listening on :{PORT}, mailroot={MAILROOT}", flush=True)
    Server(("0.0.0.0", PORT), H).serve_forever()
