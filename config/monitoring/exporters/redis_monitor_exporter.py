#!/usr/bin/env python3
"""Read-only Redis metrics exporter for the canonical Retriva monitoring stack.

Spec 035 / ADR-040. Uses ONLY the accepted `rtrv-monitor` Redis identity and a
minimal RESP client implemented with the Python standard library, so the
container needs no third-party packages.

Security properties:
- no broker/results/emergency/default credentials;
- read-only commands only (PING, INFO, EXISTS, LLEN, TYPE, SCAN, SMEMBERS,
  ACL LOG, ACL DRYRUN);
- never retrieves payloads; never exports command arguments, arbitrary key
  names, or identifiers;
- credentials come from the environment (accepted secret interface) and are
  never logged, placed in metrics/labels, or exposed via the HTTP endpoint;
- fail-closed: connection/auth failures set `redis_monitor_exporter_up=0`,
  increment an auth-failure counter and keep serving /metrics so Prometheus
  can alert on the missing signal.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("REDIS_MONITOR_HOST", "redis")
PORT = int(os.environ.get("REDIS_MONITOR_PORT", "6379"))
USERNAME = os.environ.get("REDIS_MONITOR_USERNAME", "rtrv-monitor")
PASSWORD = os.environ.get("REDIS_MONITOR_PASSWORD", "")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9187"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "15"))
SOCKET_TIMEOUT = float(os.environ.get("REDIS_SOCKET_TIMEOUT_SECONDS", "5"))
MAX_SCAN_PAGES = int(os.environ.get("MAX_SCAN_PAGES", "20"))

DESTRUCTIVE_COMMANDS = {
    "flushall", "flushdb", "swapdb", "config", "acl", "shutdown", "debug",
    "module", "migrate", "restore", "replicaof", "slaveof", "failover",
    "save", "bgsave", "bgrewriteaof", "monitor", "keys", "eval", "evalsha",
    "client", "latency", "cluster", "wait", "script", "function", "reset",
    "dump",
}
ROLE_CLASSES = {
    "rtrv-broker": "broker",
    "rtrv-results": "results",
    "rtrv-monitor": "monitor",
}


class RespError(Exception):
    pass


class RespAuthError(RespError):
    pass


class RedisClient:
    """Minimal RESP client (single connection, reconnected per poll)."""

    def __init__(self, host: str, port: int, username: str, password: str,
                 timeout: float):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._file = self._sock.makefile("rb")
        if password:
            reply = self.command("AUTH", username, password)
            if reply not in (b"OK", "OK"):
                raise RespAuthError("authentication rejected")

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()

    def _read_line(self) -> bytes:
        line = self._file.readline()
        if not line:
            raise RespError("connection closed")
        return line.rstrip(b"\r\n")

    def _read_reply(self):
        line = self._read_line()
        prefix, payload = line[:1], line[1:]
        if prefix == b"+":
            return payload.decode("utf-8", "replace")
        if prefix == b"-":
            text = payload.decode("utf-8", "replace")
            if "WRONGPASS" in text or "NOAUTH" in text or "NOPERM" in text:
                raise RespAuthError(text)
            raise RespError(text)
        if prefix == b":":
            return int(payload)
        if prefix == b"$":
            length = int(payload)
            if length == -1:
                return None
            data = self._file.read(length + 2)
            return data[:-2].decode("utf-8", "replace")
        if prefix == b"*":
            count = int(payload)
            if count == -1:
                return None
            return [self._read_reply() for _ in range(count)]
        raise RespError("unexpected reply type")

    def command(self, *args):
        parts = [b"*%d\r\n" % len(args)]
        for arg in args:
            data = str(arg).encode("utf-8")
            parts.append(b"$%d\r\n%s\r\n" % (len(data), data))
        self._sock.sendall(b"".join(parts))
        return self._read_reply()

    def scan_match(self, pattern: str, count: int = 500):
        cursor = "0"
        keys = []
        for _ in range(MAX_SCAN_PAGES):
            reply = self.command("SCAN", cursor, "MATCH", pattern, "COUNT", count)
            cursor = reply[0]
            keys.extend(reply[1])
            if cursor == "0":
                break
        return keys


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.values = {}
        self.counters = {}
        self.last_success = 0.0
        self.up = 0
        self.auth_failures = 0
        self.scrape_errors = 0

    def set(self, name: str, value: float, **labels) -> None:
        self.values[(name, tuple(sorted(labels.items())))] = value

    def set_counter(self, name: str, value: float, **labels) -> None:
        self.counters[(name, tuple(sorted(labels.items())))] = value

    def render(self) -> bytes:
        lines = []
        with self.lock:
            lines.append("# HELP redis_monitor_exporter_up 1 when the last poll succeeded")
            lines.append("# TYPE redis_monitor_exporter_up gauge")
            lines.append(f"redis_monitor_exporter_up {self.up}")
            lines.append("# HELP redis_monitor_exporter_last_success_timestamp_seconds Unix time of the last successful poll")
            lines.append("# TYPE redis_monitor_exporter_last_success_timestamp_seconds gauge")
            lines.append(
                f"redis_monitor_exporter_last_success_timestamp_seconds {self.last_success:.0f}"
            )
            lines.append("# HELP redis_monitor_exporter_auth_failures_total Exporter-side Redis authentication failures")
            lines.append("# TYPE redis_monitor_exporter_auth_failures_total counter")
            lines.append(
                f"redis_monitor_exporter_auth_failures_total {self.auth_failures}"
            )
            lines.append("# HELP redis_monitor_exporter_scrape_errors_total Exporter-side poll errors")
            lines.append("# TYPE redis_monitor_exporter_scrape_errors_total counter")
            lines.append(
                f"redis_monitor_exporter_scrape_errors_total {self.scrape_errors}"
            )
            for (name, labels), value in sorted(self.counters.items()):
                label_text = _label_text(labels)
                lines.append(f"{name}{label_text} {value}")
            for (name, labels), value in sorted(self.values.items()):
                label_text = _label_text(labels)
                lines.append(f"{name}{label_text} {_format(value)}")
        return ("\n".join(lines) + "\n").encode("utf-8")


def _label_text(labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return "{" + inner + "}"


def _format(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class Exporter:
    def __init__(self):
        self.metrics = Metrics()
        # ACL LOG accumulation state: entry-id -> last observed count.
        self._acl_entry_counts: dict[int, int] = {}
        self._acl_counters: dict[tuple, float] = {}
        # binding churn state: set key -> frozenset of members.
        self._binding_members: dict[str, set] = {}
        self._binding_churn = 0.0
        self._default_usable_prev = 0
        self._default_success = 0.0

    # -- polling -----------------------------------------------------------
    def poll_once(self) -> None:
        client = None
        try:
            client = RedisClient(HOST, PORT, USERNAME, PASSWORD, SOCKET_TIMEOUT)
            self._collect(client)
            with self.metrics.lock:
                self.metrics.up = 1
                self.metrics.last_success = time.time()
        except RespAuthError:
            with self.metrics.lock:
                self.metrics.up = 0
                self.metrics.auth_failures += 1
        except Exception:
            with self.metrics.lock:
                self.metrics.up = 0
                self.metrics.scrape_errors += 1
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _collect(self, client: RedisClient) -> None:
        m = self.metrics
        # availability
        m.set("redis_up", 1 if client.command("PING") == "PONG" else 0)
        # keyspace + persistence
        info = client.command("INFO")
        db_keys = {}
        for line in info.splitlines():
            if line.startswith("db") and ":" in line:
                try:
                    db_name, rest = line.split(":", 1)
                    fields = dict(
                        item.split("=", 1)
                        for item in rest.split(",")
                        if "=" in item
                    )
                    db_keys[db_name] = int(fields.get("keys", 0))
                except (ValueError, IndexError):
                    continue
        for db_name, count in db_keys.items():
            m.set("redis_db_keys", count, db=db_name)
        rdb_ok = 0
        for line in info.splitlines():
            if line.startswith("rdb_last_bgsave_status:"):
                rdb_ok = 1 if line.split(":", 1)[1].strip() == "ok" else 0
        m.set("redis_rdb_last_bgsave_status", rdb_ok)
        # bounded key classes
        m.set("redis_key_exists",
              1 if client.command("EXISTS", "ingestion") == 1 else 0,
              key="ingestion")
        m.set("redis_queue_depth",
              float(client.command("LLEN", "ingestion")), queue="ingestion")
        m.set("redis_unacked_present",
              1 if client.command("EXISTS", "unacked") == 1 else 0)
        m.set("redis_unacked_index_present",
              1 if client.command("EXISTS", "unacked_index") == 1 else 0)
        # binding churn
        binding_keys = client.scan_match("_kombu.binding.*")
        current = {}
        for key in binding_keys:
            members = client.command("SMEMBERS", key)
            current[key] = set(members or [])
        additions = 0
        for key, members in current.items():
            previous = self._binding_members.get(key, None)
            if previous is not None:
                additions += len(members - previous)
        self._binding_members = current
        self._binding_churn += additions
        m.set("redis_binding_sets", len(current))
        m.set_counter("redis_binding_recreation_events_total", self._binding_churn)
        # result records
        results = client.scan_match("celery-task-meta-*")
        m.set("redis_result_records_total", len(results))
        # ACL LOG aggregates
        self._collect_acl_log(client, m)
        # default/nopass usability probe (read-only)
        try:
            reply = client.command("ACL", "DRYRUN", "default", "PING")
            usable = 1 if reply == "OK" else 0
        except RespError:
            usable = 1  # ACL DRYRUN failing unexpectedly is treated as usable (fail closed upward)
        if usable == 1 and self._default_usable_prev == 0:
            self._default_success += 1
        self._default_usable_prev = usable
        m.set("redis_acl_default_usable", usable)
        m.set_counter("redis_acl_default_auth_success_total", self._default_success)

    def _collect_acl_log(self, client: RedisClient, m: Metrics) -> None:
        entries = client.command("ACL", "LOG", "128") or []
        for entry in entries:
            record = {}
            for index in range(0, len(entry) - 1, 2):
                record[entry[index]] = entry[index + 1]
            try:
                entry_id = int(record.get("entry-id", 0))
                count = int(record.get("count", 1))
            except (TypeError, ValueError):
                continue
            previous = self._acl_entry_counts.get(entry_id)
            self._acl_entry_counts[entry_id] = count
            if previous is None:
                # Adopt pre-existing entries without counting them; only growth
                # after exporter start is attributed to the counters.
                continue
            delta = max(0, count - previous)
            if delta == 0:
                continue
            reason = str(record.get("reason", "command"))
            command = str(record.get("object", "")).split("|", 1)[0].lower()
            username = str(record.get("username", ""))
            role = ROLE_CLASSES.get(username, "other")
            if reason == "auth":
                self._acl_counters[("redis_auth_failures_total", ())] = (
                    self._acl_counters.get(("redis_auth_failures_total", ()), 0.0)
                    + delta
                )
                if username == "rtrv-emergency":
                    key = ("redis_acl_emergency_use_total", ())
                    self._acl_counters[key] = self._acl_counters.get(key, 0.0) + delta
                continue
            category = "destructive" if command in DESTRUCTIVE_COMMANDS else "other"
            key = ("redis_acl_denied_commands_total",
                   (("role", role), ("category", category)))
            self._acl_counters[key] = self._acl_counters.get(key, 0.0) + delta
            if username == "rtrv-emergency":
                ekey = ("redis_acl_emergency_use_total", ())
                self._acl_counters[ekey] = self._acl_counters.get(ekey, 0.0) + delta
        # publish accumulated counters
        for (name, labels), value in self._acl_counters.items():
            m.set_counter(name, value, **dict(labels))


METRICS = Metrics()
EXPORTER = Exporter()
EXPORTER.metrics = METRICS


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/metrics"):
            body = METRICS.render()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/healthz"):
            with METRICS.lock:
                healthy = METRICS.up == 1
            self.send_response(200 if healthy else 503)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        # Never log request details that could carry query data.
        return


def main() -> None:
    if not PASSWORD:
        raise SystemExit("REDIS_MONITOR_PASSWORD is required")
    server = ThreadingHTTPServer(("0.0.0.0", EXPORTER_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    while True:
        EXPORTER.poll_once()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
