#!/usr/bin/env python3
"""Local alert sink for the canonical Retriva monitoring stack (Spec 035).

Receives Alertmanager webhook deliveries and records them locally. This is the
safe default route: it performs no external notification and holds no
receiver secrets. Operators add external receivers through the deployment's
external secret interface, never through committed values.

Endpoints:
- POST /alerts  : Alertmanager webhook payload; stored to the sink volume and
                  summarized on stdout (alertname/status only).
- GET  /healthz : 200 when the sink is running; includes received count.
- GET  /alerts  : JSON {"count": N, "alerts": ["name:status", ...]} for the
                  most recent delivery only (names/statuses; no annotations).
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("SINK_PORT", "9099"))
LOG_PATH = os.environ.get("SINK_LOG", "/sink/alerts.log")
MAX_BYTES = int(os.environ.get("SINK_MAX_BYTES", str(5 * 1024 * 1024)))

STATE_LOCK = threading.Lock()
STATE = {"count": 0, "last": []}


def _record(payload: dict) -> None:
    alerts = payload.get("alerts", []) or []
    summary = [
        f"{alert.get('labels', {}).get('alertname', 'unknown')}:"
        f"{alert.get('status', 'unknown')}"
        for alert in alerts
    ]
    with STATE_LOCK:
        STATE["count"] += len(alerts)
        STATE["last"] = summary
    line = json.dumps(payload, sort_keys=True)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_BYTES:
            # bounded retention: keep the newest half
            with open(LOG_PATH, "rb") as fh:
                fh.seek(-MAX_BYTES // 2, os.SEEK_END)
                tail = fh.read()
            with open(LOG_PATH, "wb") as fh:
                fh.write(tail)
    except OSError:
        pass
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    for item in summary:
        print(f"sink: received {item}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/alerts":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return
        if isinstance(payload, dict):
            _record(payload)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/healthz"):
            with STATE_LOCK:
                body = json.dumps({"status": "ok", "count": STATE["count"]}).encode()
            self.send_response(200)
        elif self.path.startswith("/alerts"):
            with STATE_LOCK:
                body = json.dumps(
                    {"count": STATE["count"], "alerts": list(STATE["last"])}
                ).encode()
            self.send_response(200)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


def main() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
