"""Spec 034 / ADR-039 — Redis ACL hardening configuration tests.

Deterministic, isolated, no live dependencies: they render the ACL template
with synthetic credentials (via the real entrypoint), statically validate the
role scopes and prohibitions, and check the Compose/env/runbook/monitoring
wiring committed in this repository. No real secret is required or created.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "config" / "redis" / "users.acl.template"
ENTRYPOINT = REPO / "config" / "redis" / "entrypoint.sh"
REDIS_CONF = REPO / "config" / "redis" / "redis.conf"
COMPOSE = REPO / "docker-compose.yml"
ENV_EXAMPLE = REPO / ".env.example"
RUNBOOK = REPO / "docs" / "redis-acl-runbook.md"
MONITORING = REPO / "config" / "monitoring" / "redis-alerts.proposed.yml"
OPS_SCRIPT = REPO / "scripts" / "redis-ops.sh"

SYNTHETIC = {
    "REDIS_BROKER_PASSWORD": "synthetic-broker-pw-0001",
    "REDIS_RESULTS_PASSWORD": "synthetic-results-pw-0002",
    "REDIS_MONITOR_PASSWORD": "synthetic-monitor-pw-0003",
    "REDIS_HEALTH_PASSWORD": "synthetic-health-pw-0004",
}

DENIED_COMMANDS = [
    "flushall", "flushdb", "config", "acl", "shutdown", "module", "debug",
    "migrate", "restore", "swapdb", "replicaof", "slaveof", "failover",
    "save", "bgsave", "bgrewriteaof", "monitor", "keys",
]

PLACEHOLDERS = [
    "__REDIS_BROKER_PASSWORD__", "__REDIS_RESULTS_PASSWORD__",
    "__REDIS_MONITOR_PASSWORD__", "__REDIS_HEALTH_PASSWORD__",
]


def _run_entrypoint(extra_env: dict, tmp_path: Path, base: dict | None = None):
    """Run the real entrypoint with stub setpriv/redis-server binaries."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "redis-server.argv"
    for name, body in (
        # Minimal setpriv stand-in: consume leading --options, then exec.
        ("setpriv", (
            "#!/bin/sh\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --reuid|--regid) shift 2 ;;\n"
            "    --clear-groups|--inh-caps|--ambient-caps) shift ;;\n"
            "    *) break ;;\n"
            "  esac\n"
            "done\n"
            "exec \"$@\"\n"
        )),
        ("redis-server", f"#!/bin/sh\nprintf '%s' \"$1\" > {log}\n"),
    ):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)
    out_dir = tmp_path / "acl"
    env = {
        **os.environ,
        **(SYNTHETIC if base is None else base),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "REDIS_ACL_TEMPLATE": str(TEMPLATE),
        "REDIS_ACL_OUT_DIR": str(out_dir),
        "REDIS_CONF": str(REDIS_CONF),
    }
    env.update(extra_env)
    proc = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT)], env=env, capture_output=True, text=True)
    rendered = out_dir / "users.acl"
    return proc, rendered, out_dir


def test_template_shape():
    text = TEMPLATE.read_text()
    for user in ("rtrv-broker", "rtrv-results", "rtrv-monitor", "rtrv-health"):
        assert re.search(rf"^user {user} on >__REDIS_[A-Z]+_PASSWORD__ ", text,
                         re.M), user
    assert "user default off" in text
    assert "user rtrv-emergency off" in text
    for placeholder in PLACEHOLDERS:
        assert placeholder in text
    # No real-looking secret material committed with the template.
    assert text.count(">__REDIS_") == 4


def test_rendered_acl_defaults_and_secrets(tmp_path):
    proc, rendered, out_dir = _run_entrypoint({}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    body = rendered.read_text()
    for placeholder in PLACEHOLDERS:
        assert placeholder not in body
    for user in ("rtrv-broker", "rtrv-results", "rtrv-monitor", "rtrv-health"):
        assert f"user {user} on >" in body
        assert f"synthetic-{user.split('-')[1]}-pw" in body
    assert "user default off" in body
    assert "user rtrv-emergency off" in body
    # Redis ACL files accept only user directives: comments/blank lines must
    # not survive rendering (redis-server aborts on them).
    for line in body.splitlines():
        assert line.startswith("user "), line
    mode = stat.S_IMODE(rendered.stat().st_mode)
    assert mode == 0o600
    assert (tmp_path / "redis-server.argv").read_text() == str(REDIS_CONF)


def test_normal_roles_cannot_execute_destructive_commands():
    lines = {
        "rtrv-broker": None, "rtrv-results": None,
        "rtrv-monitor": None, "rtrv-health": None,
    }
    for line in TEMPLATE.read_text().splitlines():
        for user in lines:
            if line.startswith(f"user {user} on "):
                lines[user] = line
    for user, line in lines.items():
        assert line is not None, user
        for denied in DENIED_COMMANDS:
            assert f"+{denied} " not in line + " ", (user, denied)
            assert f"|{denied} " not in line + " ", (user, denied)
        assert "+@all" not in line


def test_scripting_grant_is_broker_only_and_bounded():
    """EVALSHA/SCRIPT LOAD attribution (Spec 034 criterion 12).

    Kombu's unacked-restoration mutex acquires a redis-py Lock; its release
    uses EVALSHA + SCRIPT LOAD with the redis-py Lock release script
    (sha1 c3f8721cbb97f72bc19e972846bd7aaf91901658 — the script observed on the
    live instance). Redis 7 enforces the calling user's ACLs for commands
    invoked inside scripts, so the grant is bounded by the broker allowlist
    and must never include plain EVAL or the flush/kill subcommands.
    """
    lines = {}
    for line in TEMPLATE.read_text().splitlines():
        for user in ("rtrv-broker", "rtrv-results", "rtrv-monitor", "rtrv-health"):
            if line.startswith(f"user {user} on "):
                lines[user] = line
    broker = lines["rtrv-broker"]
    assert "+evalsha" in broker
    assert "+script|load" in broker
    for forbidden in ("+eval ", "+script ", "script|flush", "script|kill"):
        assert forbidden not in broker + " ", forbidden
    for user in ("rtrv-results", "rtrv-monitor", "rtrv-health"):
        line = lines[user]
        for scripting in ("+eval", "+evalsha", "+script", "script|load"):
            assert scripting not in line, (user, scripting)


def test_broker_mutex_and_queue_commands():
    """Commands required by kombu under real traffic (isolated validation)."""
    broker = next(line for line in TEMPLATE.read_text().splitlines()
                  if line.startswith("user rtrv-broker on "))
    for cmd in ("+get", "+set", "+llen", "+watch", "+unwatch", "+lpush",
                "+rpush", "+evalsha", "+script|load"):
        assert f"{cmd} " in broker + " ", cmd
    monitor = next(line for line in TEMPLATE.read_text().splitlines()
                   if line.startswith("user rtrv-monitor on "))
    assert "+acl|log" in monitor
    assert "+client|setinfo" in monitor
    health = next(line for line in TEMPLATE.read_text().splitlines()
                  if line.startswith("user rtrv-health on "))
    assert "+client|setinfo" in health
    assert "+acl" not in health


def test_broker_and_results_scopes():
    broker = next(line for line in TEMPLATE.read_text().splitlines()
                  if line.startswith("user rtrv-broker on "))
    results = next(line for line in TEMPLATE.read_text().splitlines()
                   if line.startswith("user rtrv-results on "))
    for pattern in ("~ingestion* ", "~_kombu.binding.*", "~unacked ",
                    "~unacked_index", "~unacked_mutex",
                    "~*.celery.pidbox*", "~celeryev.*"):
        assert pattern in broker
    assert "~celery-task-meta-*" not in broker
    assert "&/0.celeryev" in broker and "&/0.celeryev/*" in broker
    # PSUBSCRIBE requires the exact allowed pattern (Redis ACL semantics).
    assert "&/0.celeryev/worker.*" in broker
    assert "&/0.celery.pidbox" in broker and "&/0.celery.pidbox/*" in broker
    assert "~celery-task-meta-*" in results
    assert "~ingestion" not in results
    assert "&celery-task-meta-*" in results


def test_entrypoint_fail_closed(tmp_path):
    base = dict(SYNTHETIC)
    del base["REDIS_RESULTS_PASSWORD"]
    proc, rendered, _ = _run_entrypoint({}, tmp_path / "missing", base=base)
    assert proc.returncode != 0
    assert not rendered.exists()
    bad = dict(SYNTHETIC)
    bad["REDIS_BROKER_PASSWORD"] = "bad char$"
    proc, rendered, _ = _run_entrypoint({}, tmp_path / "badchar", base=bad)
    assert proc.returncode != 0
    assert not rendered.exists()
    proc, rendered, _ = _run_entrypoint(
        {"REDIS_ALLOW_UNAUTHENTICATED_DEFAULT": "maybe"}, tmp_path / "badflag")
    assert proc.returncode != 0


def test_staged_rollout_flag_never_production_default(tmp_path):
    proc, rendered, _ = _run_entrypoint(
        {"REDIS_ALLOW_UNAUTHENTICATED_DEFAULT": "yes"}, tmp_path / "staged")
    assert proc.returncode == 0
    assert "user default on nopass" in rendered.read_text()
    proc, rendered, _ = _run_entrypoint({}, tmp_path / "final")
    assert "user default off" in rendered.read_text()


def test_compose_redis_hardening():
    compose = yaml.safe_load(COMPOSE.read_text())
    redis = compose["services"]["redis"]
    assert redis["ports"] == ["127.0.0.1:${REDIS_PORT:-6379}:6379"]
    assert "0.0.0.0" not in str(redis["ports"])
    env = redis["environment"]
    for key in ("REDIS_BROKER_PASSWORD", "REDIS_RESULTS_PASSWORD",
                "REDIS_MONITOR_PASSWORD", "REDIS_HEALTH_PASSWORD"):
        assert ":?" in env[key], key
    assert env["REDIS_ALLOW_UNAUTHENTICATED_DEFAULT"].endswith(":-no}")
    hc = redis["healthcheck"]["test"][1]
    assert "REDISCLI_AUTH" in hc and "--user rtrv-health" in hc
    assert "$$REDIS_HEALTH_PASSWORD" in hc  # compose-escaped; resolved at runtime
    mounts = "\n".join(str(v) for v in redis["volumes"])
    for name in ("entrypoint.sh", "redis.conf", "users.acl.template"):
        assert name in mounts
    assert redis["entrypoint"] == ["/bin/sh", "/etc/redis/entrypoint.sh"]


def test_compose_service_identities():
    compose = yaml.safe_load(COMPOSE.read_text())
    for service in ("retriva-ingestion", "retriva-worker"):
        env = compose["services"][service]["environment"]
        assert "rtrv-broker:${REDIS_BROKER_PASSWORD:?" in env["CELERY_BROKER_URL"]
        assert "rtrv-results:${REDIS_RESULTS_PASSWORD:?" in env["CELERY_RESULT_BACKEND"]
        assert "/0" in env["CELERY_BROKER_URL"] and "/1" in env["CELERY_RESULT_BACKEND"]


def test_env_example_secret_names_only():
    text = ENV_EXAMPLE.read_text()
    for key in ("REDIS_BROKER_PASSWORD", "REDIS_RESULTS_PASSWORD",
                "REDIS_MONITOR_PASSWORD", "REDIS_HEALTH_PASSWORD"):
        assert re.search(rf"^{key}=$", text, re.M), key
    assert re.search(r"^REDIS_ALLOW_UNAUTHENTICATED_DEFAULT=no$", text, re.M)


def test_runbook_has_no_executable_destructive_commands():
    text = RUNBOOK.read_text()
    for required in ("no-flush", "FLUSHALL", "rtrv-emergency",
                     "I-AM-OPERATING-ON", "ACL LOG", "rotation"):
        assert required.lower() in text.lower(), required
    forbidden = re.compile(
        r"^\s*(sudo\s+)?(docker\s+exec\s+\S+\s+)?redis-cli[^\n]*"
        r"(flushall|flushdb)", re.IGNORECASE | re.MULTILINE)
    assert not forbidden.search(text)


def test_ops_script_is_read_only():
    text = OPS_SCRIPT.read_text()
    assert "ACL DRYRUN" in text
    assert "expect denied" in text
    direct = re.compile(
        r"(redis-cli|\bR\b)[^\n]*"
        r"(flushall|flushdb|config\s+set|acl\s+setuser|acl\s+deluser)",
        re.IGNORECASE)
    assert not direct.search(text)


def test_monitoring_rules_valid():
    doc = yaml.safe_load(MONITORING.read_text())
    names = [rule["alert"] for group in doc["groups"] for rule in group["rules"]]
    for expected in ("RedisDeniedDestructiveCommand", "RedisTotalKeyCollapse",
                     "RedisQueueDisappearance", "RedisEmergencyIdentityUse",
                     "RedisDefaultOrNopassAuthSuccess"):
        assert expected in names, expected
