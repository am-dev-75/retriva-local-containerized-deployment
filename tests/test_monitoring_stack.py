# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic tests for the canonical monitoring stack (Spec 035 / ADR-040).

Covers compose/config validity, pinned images, private exposure, persistence and
retention, secret fail-closed behavior, read-only collector contracts, bounded
labels, rule syntax and the 15 synthetic alert cases (via pinned promtool when
Docker is available), self-monitoring, runbook references, Alertmanager routing
validity, and rollback compatibility.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
CONFIG = ROOT / "config" / "monitoring"
RULES = CONFIG / "prometheus" / "rules"
RUNBOOK = ROOT / "docs" / "monitoring-runbook.md"

PROM_IMAGE = "prom/prometheus:v3.15.0"
AM_IMAGE = "prom/alertmanager:v0.34.1"
MONITORING_SERVICES = {
    "retriva-prometheus",
    "retriva-alertmanager",
    "retriva-alert-sink",
    "retriva-redis-monitor-exporter",
    "retriva-pg-monitor-exporter",
}
REDIS_METRICS = {
    "redis_monitor_exporter_up",
    "redis_up",
    "redis_db_keys",
    "redis_key_exists",
    "redis_queue_depth",
    "redis_unacked_present",
    "redis_unacked_index_present",
    "redis_binding_sets",
    "redis_binding_recreation_events_total",
    "redis_result_records_total",
    "redis_acl_denied_commands_total",
    "redis_auth_failures_total",
    "redis_acl_emergency_use_total",
    "redis_acl_default_auth_success_total",
    "redis_acl_default_usable",
    "redis_rdb_last_bgsave_status",
    "redis_monitor_exporter_auth_failures_total",
}
PG_METRICS = {
    "retriva_pg_nonterminal_jobs",
    "retriva_pg_monitor_up",
    "retriva_pg_monitor_last_success_timestamp_seconds",
    "retriva_pg_monitor_query_errors_total",
}
REQUIRED_METRICS = REDIS_METRICS | PG_METRICS


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _rules(path: Path) -> list:
    return yaml.safe_load(path.read_text())["groups"][0]["rules"]


# ---------------------------------------------------------------- compose ----

def test_monitoring_services_defined_with_profile():
    services = _compose()["services"]
    monitoring = {
        name for name, spec in services.items()
        if isinstance(spec, dict) and "monitoring" in (spec.get("profiles") or [])
    }
    assert monitoring == MONITORING_SERVICES


def test_monitoring_images_are_pinned():
    services = _compose()["services"]
    assert services["retriva-prometheus"]["image"] == (
        "${PROMETHEUS_IMAGE:-" + PROM_IMAGE + "}"
    )
    assert services["retriva-alertmanager"]["image"] == (
        "${ALERTMANAGER_IMAGE:-" + AM_IMAGE + "}"
    )
    for name in MONITORING_SERVICES:
        image = services[name]["image"]
        assert ":latest" not in image, name


def test_monitoring_services_publish_no_host_ports():
    for name in MONITORING_SERVICES:
        spec = _compose()["services"][name]
        assert "ports" not in spec, f"{name} must not publish host ports"
        assert "retriva-net" in (spec.get("networks") or [])


def test_monitoring_persistence_and_retention():
    services = _compose()["services"]
    volumes = _compose()["volumes"]
    for volume in ("prometheus_data", "alertmanager_data", "alert_sink_data"):
        assert volume in volumes
    prom = services["retriva-prometheus"]
    assert "--storage.tsdb.path=/prometheus" in prom["command"]
    retention = [c for c in prom["command"] if "retention.time" in c]
    assert retention and "${PROMETHEUS_RETENTION_TIME:-30d}" in retention[0]
    assert "prometheus_data:/prometheus" in prom["volumes"]


def test_monitoring_healthchecks_present():
    services = _compose()["services"]
    for name in MONITORING_SERVICES:
        assert "healthcheck" in services[name], name


def test_monitoring_secrets_fail_closed():
    services = _compose()["services"]
    redis_env = services["retriva-redis-monitor-exporter"]["environment"]
    assert ":?" in redis_env["REDIS_MONITOR_PASSWORD"]
    pg_env = services["retriva-pg-monitor-exporter"]["environment"]
    assert ":?" in pg_env["PGUSER"]
    assert ":?" in pg_env["PGPASSWORD"]
    assert pg_env["PGOPTIONS"].endswith("statement_timeout=5000")


# --------------------------------------------------------------- exporters ----

def test_redis_exporter_is_read_only_and_monitor_only():
    script = (CONFIG / "exporters" / "redis_monitor_exporter.py").read_text()
    for forbidden in (
        '"SET"', '"SETEX"', '"DEL"', '"FLUSHALL"', '"FLUSHDB"', '"EVAL"',
        '"CONFIG"', '"ACL", "SETUSER"', '"SHUTDOWN"',
    ):
        assert forbidden not in script, forbidden
    assert 'os.environ.get("REDIS_MONITOR_USERNAME", "rtrv-monitor")' in script
    for metric in REDIS_METRICS:
        assert metric in script, metric


def test_pg_collector_is_aggregate_only():
    script = (CONFIG / "exporters" / "pg_monitor_collector.sh").read_text()
    assert "SELECT count(*) FROM jobs.jobs" in script
    assert "statement_timeout=5000" in script
    for metric in PG_METRICS:
        assert metric in script, metric
    for forbidden in ("tenant_id", "job_id", "attempt_id", "task_id"):
        assert forbidden not in script, forbidden


def test_metric_contract_documents_labels_and_forbidden_data():
    contract = (CONFIG / "metric-contract.md").read_text()
    for metric in REQUIRED_METRICS:
        assert f"`{metric}`" in contract, metric
    assert "No metric, label, annotation," in contract
    assert "payloads" in contract.lower()
    assert "role` ∈ {broker,results,monitor,other}" in contract
    assert "category` ∈ {destructive,other}" in contract


# ------------------------------------------------------------------- rules ----

def test_redis_rule_group_shape():
    rules = _rules(RULES / "redis-security.rules.yml")
    by_name = {rule["alert"]: rule for rule in rules}
    assert len(by_name) == 9
    for name in (
        "RedisDeniedDestructiveCommand",
        "RedisAuthFailuresAboveBaseline",
        "RedisTotalKeyCollapse",
        "RedisQueueDisappearance",
        "RedisBindingRecreationSpike",
        "RedisResultRecordCollapse",
        "RedisRestartOrPersistenceAnomaly",
        "RedisEmergencyIdentityUse",
        "RedisDefaultOrNopassAuthSuccess",
    ):
        rule = by_name[name]
        assert rule["for"], name
        assert rule["labels"]["severity"] in {"critical", "warning"}, name
        assert rule["labels"]["service"] == "redis", name
        assert rule["labels"]["owner"] == "monitoring-stack-owner", name
        assert rule["annotations"]["summary"], name
        assert rule["annotations"]["description"], name
        assert rule["annotations"]["runbook"].startswith(
            "docs/monitoring-runbook.md#"
        ), name


def test_rule_annotations_are_redacted():
    for rule in _rules(RULES / "redis-security.rules.yml"):
        text = " ".join(str(v) for v in rule["annotations"].values()) + str(rule["expr"])
        lowered = text.lower()
        for forbidden in ("password", "://", "token", "secret", "@"):
            assert forbidden not in lowered, (rule["alert"], forbidden)
        # no identifier-like values (job ids, task ids, tenants, documents)
        assert not re.search(
            r"\b(?:job|task|attempt|document|tenant|version)[-_][a-z0-9]{4,}", lowered
        ), rule["alert"]


def test_self_monitoring_rule_group_shape():
    rules = _rules(RULES / "monitoring-self.rules.yml")
    names = {rule["alert"] for rule in rules}
    assert {
        "MonitoringCollectorScrapeDown",
        "MonitoringRuleEvaluationFailures",
        "MonitoringAlertmanagerDown",
        "MonitoringRedisExporterAuthFailures",
        "MonitoringPostgresGaugeStale",
        "MonitoringConfigReloadFailed",
        "MonitoringStorageHighUsage",
    } <= names
    for rule in rules:
        assert rule["labels"]["service"] == "monitoring"
        assert rule["labels"]["owner"] == "monitoring-stack-owner"
        assert rule["for"]


def test_alertmanager_config_valid_and_safe_default_route():
    config = yaml.safe_load((CONFIG / "alertmanager" / "alertmanager.yml").read_text())
    receivers = {receiver["name"]: receiver for receiver in config["receivers"]}
    assert "monitoring-sink" in receivers
    webhook = receivers["monitoring-sink"]["webhook_configs"][0]
    assert webhook["url"] == "http://alert-sink:9099/alerts"
    assert webhook["send_resolved"] is True
    assert config["route"]["receiver"] == "monitoring-sink"
    assert config["route"]["group_by"]
    assert config["inhibit_rules"]
    text = (CONFIG / "alertmanager" / "alertmanager.yml").read_text().lower()
    for forbidden in ("smtp", "slack_api_url", "pagerduty", "opsgenie", "@"):
        if forbidden == "@":
            continue
        assert forbidden not in text, forbidden


def test_runbook_anchors_exist_for_rule_references():
    runbook_text = RUNBOOK.read_text()
    headings = []
    for line in runbook_text.splitlines():
        if line.startswith("## "):
            slug = re.sub(r"[^a-z0-9 -]", "", line[3:].lower())
            headings.append(slug.replace(" ", "-"))
    for rules_file in RULES.glob("*.rules.yml"):
        for rule in _rules(rules_file):
            anchor = rule["annotations"]["runbook"].split("#", 1)[1]
            assert anchor in headings, (rule["alert"], anchor)


def test_runbook_contains_required_sections():
    text = RUNBOOK.read_text()
    for section in (
        "Destructive-test prohibition",
        "Durable PostgreSQL reconciliation",
        "Stale Redis restore prohibition",
        "Escalation",
        "Silencing and maintenance",
        "Rollback / removal",
        "Safe evidence collection",
        "FLUSHALL",
    ):
        assert section in text, section


# ------------------------------------------------- promtool-based rule tests ----

def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _image_present(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
    )
    return result.returncode == 0


def _promtool(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{RULES}:/rules:ro",
            "-w", "/rules",
            "--entrypoint", "promtool",
            PROM_IMAGE, *args,
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(not _docker_available(), reason="docker unavailable")
def test_promtool_checks_and_unit_tests():
    if not _image_present(PROM_IMAGE):
        pytest.skip(f"{PROM_IMAGE} not available locally")
    check = _promtool(["check", "rules", "redis-security.rules.yml"])
    assert check.returncode == 0, check.stdout + check.stderr
    assert "SUCCESS: 9 rules found" in check.stdout
    test = _promtool(["test", "rules", "redis-security.rules.test.yml"])
    assert test.returncode == 0, test.stdout + test.stderr
    assert "SUCCESS" in test.stdout

    check_self = _promtool(["check", "rules", "monitoring-self.rules.yml"])
    assert check_self.returncode == 0, check_self.stdout + check_self.stderr
    assert "SUCCESS: 7 rules found" in check_self.stdout
    test_self = _promtool(["test", "rules", "monitoring-self.rules.test.yml"])
    assert test_self.returncode == 0, test_self.stdout + test_self.stderr
    assert "SUCCESS" in test_self.stdout


@pytest.mark.skipif(not _docker_available(), reason="docker unavailable")
def test_alertmanager_config_via_amtool():
    if not _image_present(AM_IMAGE):
        pytest.skip(f"{AM_IMAGE} not available locally")
    result = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{CONFIG / 'alertmanager'}:/cfg:ro",
            "--entrypoint", "amtool",
            AM_IMAGE, "check-config", "/cfg/alertmanager.yml",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------- rollback compat ----

def test_monitoring_profile_is_additive():
    services = _compose()["services"]
    # Monitoring services must all be profile-gated so a default `up` is
    # unchanged; their removal therefore leaves the application stack intact.
    for name in MONITORING_SERVICES:
        assert "monitoring" in services[name]["profiles"]
        assert name not in {"redis", "retriva-worker", "retriva-ingestion"}


def test_no_secrets_committed_in_monitoring_config():
    for path in CONFIG.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="ignore")
            for pattern in (
                r"rtrv-broker:[^@\s]+@",
                r"rtrv-results:[^@\s]+@",
                r"rtrv-monitor:[^@\s]+@",
                r"rtrv-health:[^@\s]+@",
                r"password\s*=\s*[A-Za-z0-9_-]{8,}",
            ):
                assert not re.search(pattern, text), (str(path), pattern)
