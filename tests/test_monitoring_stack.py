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
    # The PostgreSQL collector uses a locally built derived image (repo
    # convention for deployment-owned images): base pinned postgres plus the
    # busybox-extras httpd applet the base image lacks.
    assert services["retriva-pg-monitor-exporter"]["build"]["dockerfile"] == (
        "Dockerfile.pg-monitor"
    )
    assert (CONFIG / "exporters" / "Dockerfile.pg-monitor").exists()
    assert "postgres:16.15-alpine" in (
        CONFIG / "exporters" / "Dockerfile.pg-monitor"
    ).read_text()
    assert "busybox-extras" in (
        CONFIG / "exporters" / "Dockerfile.pg-monitor"
    ).read_text()
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


def test_rule_metric_names_are_emitted_by_collectors():
    # The validated A3a expression uses redis_db_keys_total; the result-record
    # rule needs a DB1 scan.  Both are regression points from isolated
    # validation.
    script = (CONFIG / "exporters" / "redis_monitor_exporter.py").read_text()
    assert "redis_db_keys_total" in script
    assert 'client.command("SELECT", "1")' in script
    assert "no password is set for the default user" in script  # nopass detection


def test_prometheus_links_alertmanager_and_inhibition_exempts_nopass():
    prom = (CONFIG / "prometheus" / "prometheus.yml").read_text()
    assert "alerting:" in prom
    assert "retriva-alertmanager:9093" in prom
    am = (CONFIG / "alertmanager" / "alertmanager.yml").read_text()
    assert am.count('alertname!="RedisDefaultOrNopassAuthSuccess"') == 2


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
    assert webhook["url"] == "http://retriva-alert-sink:9099/alerts"
    assert webhook["send_resolved"] is True
    assert config["route"]["receiver"] == "monitoring-sink"
    assert config["route"]["group_by"]
    assert config["inhibit_rules"]
    text = (CONFIG / "alertmanager" / "alertmanager.yml").read_text().lower()
    for forbidden in ("smtp", "slack_api_url", "pagerduty", "opsgenie", "@"):
        if forbidden == "@":
            continue
        assert forbidden not in text, forbidden


# ------------------------------------------------- canonical topology ----
#
# One canonical naming source: the Compose model. Every hostname used by the
# committed monitoring configuration must be a Compose service name or a
# declared network alias of this repository, so a config can never again
# reference an isolated-harness-only name. Reachability of these names is
# proven live in the canonical isolated end-to-end validation; these tests
# prove resolution against the declared model deterministically.

HARNESS_ONLY_HOSTNAMES = {
    "alertmanager",
    "alert-sink",
    "redis-monitor-exporter",
    "pg-monitor-exporter",
    "prometheus",
}


def _compose_declared_names() -> set:
    """Every DNS name the canonical Compose network registers: service names
    plus per-service network aliases."""
    names = set()
    for service, spec in _compose()["services"].items():
        names.add(service)
        if isinstance(spec, dict):
            networks = spec.get("networks") or []
            if isinstance(networks, dict):
                entries = networks.values()
            else:
                entries = networks
            for entry in entries:
                if isinstance(entry, dict):
                    names.update(entry.get("aliases") or [])
    return names


def _hostname_of(target: str) -> str:
    return target.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]


def _port_of(target: str) -> str:
    hostport = target.split("://", 1)[-1].split("/", 1)[0]
    return hostport.split(":", 1)[1] if ":" in hostport else ""


def _monitoring_config_hostnames() -> list:
    prom = yaml.safe_load(
        (CONFIG / "prometheus" / "prometheus.yml").read_text()
    )
    targets = []
    for entry in prom.get("alerting", {}).get("alertmanagers", []):
        for cfg in entry.get("static_configs", []):
            targets.extend(cfg.get("targets", []))
    for job in prom.get("scrape_configs", []):
        for cfg in job.get("static_configs", []):
            targets.extend(cfg.get("targets", []))
    am = yaml.safe_load(
        (CONFIG / "alertmanager" / "alertmanager.yml").read_text()
    )
    urls = []
    for receiver in am.get("receivers", []):
        for webhook in receiver.get("webhook_configs", []) or []:
            urls.append(webhook["url"])
    return targets, urls


def test_monitoring_targets_resolve_to_canonical_compose_names():
    declared = _compose_declared_names()
    targets, urls = _monitoring_config_hostnames()
    assert targets, "prometheus must define targets"
    for target in targets + urls:
        host = _hostname_of(target)
        # the only permitted non-service hostname is the Prometheus
        # self-scrape loopback target
        if host in {"localhost", "127.0.0.1"}:
            assert _port_of(target) == "9090", target
            continue
        assert host in declared, (
            f"{host!r} from {target!r} is not a canonical Compose service "
            f"name or declared alias: registered names are {sorted(declared)}"
        )


def test_no_isolated_harness_hostnames_in_production_config():
    targets, urls = _monitoring_config_hostnames()
    offenders = [
        t for t in targets + urls if _hostname_of(t) in HARNESS_ONLY_HOSTNAMES
    ]
    assert not offenders, (
        "isolated-harness-only hostnames are forbidden in the committed "
        f"monitoring configuration: {offenders}"
    )
    # Defensive raw check: the exact harness-only endpoints must never
    # reappear anywhere in the committed monitoring tree (word-boundary
    # safe: `retriva-alertmanager:9093` is canonical and permitted).
    for path in CONFIG.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        for endpoint in (
            "alertmanager:9093",
            "alert-sink:9099",
            "redis-monitor-exporter:9187",
            "pg-monitor-exporter:9188",
        ):
            pattern = r"(?<![\w-])" + re.escape(endpoint)
            assert not re.search(pattern, text), (str(path), endpoint)


def test_monitoring_target_ports_match_declared_compose_env():
    services = _compose()["services"]
    targets, urls = _monitoring_config_hostnames()
    redis_target = [t for t in targets if "redis-monitor" in t][0]
    pg_target = [t for t in targets if "pg-monitor" in t][0]
    assert _port_of(redis_target) == services[
        "retriva-redis-monitor-exporter"
    ]["environment"]["EXPORTER_PORT"]
    assert _port_of(pg_target) == services[
        "retriva-pg-monitor-exporter"
    ]["environment"]["METRICS_PORT"]
    assert _port_of(urls[0]) == services["retriva-alert-sink"][
        "environment"
    ]["SINK_PORT"]
    assert _hostname_of(urls[0]) == "retriva-alert-sink"


def test_collector_dependency_hosts_are_canonical_compose_services():
    declared = _compose_declared_names()
    services = _compose()["services"]
    redis_host = services["retriva-redis-monitor-exporter"][
        "environment"
    ]["REDIS_MONITOR_HOST"]
    pg_host = services["retriva-pg-monitor-exporter"]["environment"]["PGHOST"]
    assert redis_host in declared
    assert pg_host in declared


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


def test_runbook_uses_canonical_topology_and_dependency_safe_commands():
    text = RUNBOOK.read_text()
    five = (
        "retriva-prometheus retriva-alertmanager retriva-alert-sink "
        "retriva-redis-monitor-exporter retriva-pg-monitor-exporter"
    )
    # dependency-safe deploy, and monitoring-only stop/remove
    assert f"docker compose --profile monitoring up -d --no-deps {five}" in text
    assert f"docker compose --profile monitoring stop {five}" in text
    assert f"docker compose --profile monitoring rm -sf {five}" in text
    # a bare profile `down` would also remove base-profile services; the
    # runbook must never present it as an allowed command.
    assert "--profile monitoring down" not in text
    assert "down" in text  # the prohibition itself is documented
    for name in MONITORING_SERVICES:
        assert name in text, name


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
