"""Deployment-structure tests for the shared PostgreSQL platform
(Spec 024; ADR-029).

Deterministic: uses `docker compose config` (no daemon required for
resolution) plus plain file assertions.  Skips the resolution checks
when the docker CLI is unavailable.

Validates:

- PostgreSQL is MANDATORY: the base (profile-less) resolution includes
  the full Core lifecycle (postgres -> bootstrap -> migrate) and the
  core services depend on Core migration success;
- the Core-only path contains no Pro service or Pro credential;
- the Pro profile adds the CRM one-shots AFTER the Core migrations,
  and no Core service depends on them;
- the retired dedicated Messaging PostgreSQL service/database target is
  gone; Messaging uses the shared `retriva-postgres`/`retriva` database
  and its service starts only after its own migration one-shot;
- least-credential posture of every one-shot, pgAdmin localhost bind,
  pinned images, health checks without credentials, the .env.example
  contract, and the manage.sh command surface.

Run:  python3 -m pytest tests/test_postgres_deployment.py -q
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
MANAGE = ROOT / "scripts" / "manage.sh"
PGADMIN_SERVERS = ROOT / "config" / "pgadmin" / "servers.json"

PLATFORM_SERVICES = (
    "retriva-postgres",
    "retriva-pg-bootstrap",
    "retriva-pg-migrate",
)
PRO_PG_SERVICES = (
    "retriva-pg-crm-bootstrap",
    "retriva-pg-crm-migrate",
    "retriva-pgadmin",
)
MESSAGING_PG_SERVICES = (
    "retriva-pg-messaging-bootstrap",
    "retriva-pg-messaging-migrate",
)
CORE_SERVICES = (
    "retriva-ingestion",
    "retriva-core",
    "retriva-worker",
    "retriva-gateway",
)


def _compose_available() -> bool:
    return shutil.which("docker") is not None


def _resolve(profiles=()):
    """Resolve the compose file with the example env (no daemon)."""
    env_file = HERE.parent / ".env.example"
    cmd = ["docker", "compose", "-f", str(COMPOSE),
           "--env-file", str(env_file)]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += ["config"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        env={**os.environ, "ENV_FILE": str(env_file)})
    return proc


def _services(resolved: str):
    """Parse the resolved compose model into a services mapping."""
    return yaml.safe_load(resolved)["services"]


class MandatoryPostgresLifecycle(unittest.TestCase):

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_base_resolution_includes_mandatory_pg_lifecycle(self):
        proc = _resolve()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        services = _services(proc.stdout)
        for name in PLATFORM_SERVICES:
            self.assertIn(name, services,
                          f"{name} must be in the profile-less base set")
        # pgAdmin stays an optional operator tool.
        for name in PRO_PG_SERVICES + MESSAGING_PG_SERVICES:
            self.assertNotIn(name, services)

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_core_services_depend_on_core_migration_success(self):
        proc = _resolve()
        services = _services(proc.stdout)
        for name in CORE_SERVICES:
            deps = services[name]["depends_on"]
            self.assertEqual(
                deps.get("retriva-pg-migrate", {}).get("condition"),
                "service_completed_successfully",
                f"{name} must start only after Core migrations succeed")

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_pg_lifecycle_order_and_idempotent_one_shots(self):
        proc = _resolve()
        services = _services(proc.stdout)
        self.assertEqual(
            services["retriva-pg-bootstrap"]["depends_on"]
            ["retriva-postgres"]["condition"], "service_healthy")
        self.assertEqual(
            services["retriva-pg-migrate"]["depends_on"]
            ["retriva-pg-bootstrap"]["condition"],
            "service_completed_successfully")
        for name in PLATFORM_SERVICES[1:]:
            self.assertEqual(services[name].get("restart"), "no",
                             f"{name} must be a one-shot (no restart loop)")
        # No arbitrary sleeps in any directive of the lifecycle.
        for line in COMPOSE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("sleep", stripped.lower(),
                              f"arbitrary sleep in compose: {line}")

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_postgres_volume_preserved_and_no_published_ports(self):
        proc = _resolve()
        services = _services(proc.stdout)
        pg = services["retriva-postgres"]
        self.assertNotIn("ports", pg,
                          "retriva-postgres must be internal-network only")
        self.assertIn("retriva-net", str(pg))
        volumes = yaml.safe_load(proc.stdout)["volumes"]
        self.assertIn("retriva_pg_data", volumes,
                      "the existing development data volume must survive")

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_health_check_exposes_no_credentials(self):
        proc = _resolve()
        services = _services(proc.stdout)
        health = services["retriva-postgres"]["healthcheck"]["test"]
        rendered = " ".join(health)
        self.assertIn("pg_isready", rendered)
        self.assertNotIn("PASSWORD", rendered.upper())
        self.assertNotIn(":", rendered.split("-U")[1].split()[0] + " ")


class ProExtensionLifecycle(unittest.TestCase):

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_pro_profile_orders_extension_migrations_after_core(self):
        # "db" is enabled so pgAdmin (an optional operator tool) is part
        # of the resolution; the Pro one-shots themselves are in "pro".
        proc = _resolve(profiles=["pro", "db"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        services = _services(proc.stdout)
        for name in PLATFORM_SERVICES:
            self.assertIn(name, services)
        for name in PRO_PG_SERVICES:
            self.assertIn(name, services)
        # CRM one-shots run after the Core lifecycle and the CRM
        # bootstrap, never before it.
        self.assertEqual(
            services["retriva-pg-crm-migrate"]["depends_on"]
            ["retriva-pg-migrate"]["condition"],
            "service_completed_successfully")
        self.assertEqual(
            services["retriva-pg-crm-migrate"]["depends_on"]
            ["retriva-pg-crm-bootstrap"]["condition"],
            "service_completed_successfully")
        self.assertEqual(
            services["retriva-pg-crm-bootstrap"]["depends_on"]
            ["retriva-pg-bootstrap"]["condition"],
            "service_completed_successfully")
        # Core services never depend on the optional Pro one-shots.
        for core in CORE_SERVICES:
            deps = services[core]["depends_on"]
            self.assertNotIn("retriva-pg-crm-migrate", deps)
            self.assertNotIn("retriva-pg-crm-bootstrap", deps)

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_platform_one_shots_build_the_core_only_base_stage(self):
        """True Core-only path (§3.1): the platform one-shots build the
        Core-only `base` stage (never the Pro stage), while the CRM
        one-shots build the `pro` stage, and the core services default
        to `base` (the genuine Core-only image) with `pro` reserved for
        the Pro development workflow."""
        proc = _resolve()
        services = _services(proc.stdout)
        for name in ("retriva-pg-bootstrap", "retriva-pg-migrate"):
            target = services[name]["build"]["target"]
            # Resolved from the example env: no override set -> base.
            self.assertIn(
                "base", str(target),
                f"{name} must build the Core-only base stage, got "
                f"{target!r}")
        # Core services default to base too (RETRIVA_CORE_BUILD_TARGET
        # overrides them to pro for the Pro workflow).
        for name in CORE_SERVICES:
            self.assertIn(
                "base", str(services[name]["build"]["target"]),
                f"{name} must default to the Core-only base stage")
        # The CRM one-shots always build the Pro stage.
        proc = _resolve(profiles=["pro"])
        services = _services(proc.stdout)
        for name in ("retriva-pg-crm-bootstrap", "retriva-pg-crm-migrate"):
            self.assertIn(
                "pro", str(services[name]["build"]["target"]),
                f"{name} must build the Pro stage")

    def test_dockerfile_declares_the_base_stage(self):
        """The Core Dockerfile must name its Core-only stage `base`
        (the compose default target) — same convention as the gateway
        Dockerfile."""
        core_dockerfile = (ROOT.parent / "retriva-core" / "Dockerfile")
        text = core_dockerfile.read_text(encoding="utf-8")
        self.assertRegex(text, r"FROM python:3\.12-slim AS base")
        self.assertRegex(text, r"FROM python:3\.12-slim AS pro")

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_messaging_targets_the_shared_database(self):
        proc = _resolve(profiles=["pro", "messaging"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        services = _services(proc.stdout)
        # The dedicated Messaging PostgreSQL service is gone.
        self.assertNotIn("retriva-messaging-db", services)
        self.assertNotIn("messaging_db_data",
                          yaml.safe_load(proc.stdout).get("volumes", {}))
        for name in MESSAGING_PG_SERVICES:
            self.assertIn(name, services)
        # Messaging runs against the shared instance/database...
        url = services["retriva-messaging"]["environment"][
            "RETRIVA_MESSAGING_DATABASE_URL"]
        self.assertIn("@retriva-postgres:5432/retriva", url)
        self.assertNotIn("retriva_messaging@", url.replace(
            "retriva_messaging:", "", 1))  # no dedicated DB user default
        self.assertNotIn("retriva-messaging-db", url)
        # ...and its service starts only after its own migrations.
        self.assertEqual(
            services["retriva-messaging"]["depends_on"]
            ["retriva-pg-messaging-migrate"]["condition"],
            "service_completed_successfully")
        self.assertEqual(
            services["retriva-pg-messaging-migrate"]["depends_on"]
            ["retriva-pg-migrate"]["condition"],
            "service_completed_successfully")


class LeastPrivilegePosture(unittest.TestCase):

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_one_shots_use_least_privilege(self):
        proc = _resolve(profiles=["pro"])
        services = _services(proc.stdout)
        # Core migrate: migrator credential ONLY (no admin, no roles).
        core_mig = services["retriva-pg-migrate"]["environment"]
        self.assertIn("RETRIVA_PG_MIGRATOR_PASSWORD", core_mig)
        self.assertNotIn("RETRIVA_PG_ADMIN_PASSWORD", core_mig)
        self.assertNotIn("CRM_PG_APPLICATION_PASSWORD", core_mig)
        # Core migrate registers no extension provider (Core-only).
        self.assertNotIn("RETRIVA_PG_MIGRATION_PROVIDERS", core_mig)
        # CRM migrate: migrator credential only.
        crm_mig = services["retriva-pg-crm-migrate"]["environment"]
        self.assertIn("RETRIVA_PG_MIGRATOR_PASSWORD", crm_mig)
        self.assertNotIn("RETRIVA_PG_ADMIN_PASSWORD", crm_mig)
        # Bootstraps carry the admin credential (deployment-time only).
        self.assertIn("RETRIVA_PG_ADMIN_PASSWORD",
                      services["retriva-pg-bootstrap"]["environment"])
        self.assertIn("RETRIVA_PG_ADMIN_PASSWORD",
                      services["retriva-pg-crm-bootstrap"]["environment"])
        # The Core bootstrap provisions only the platform roles.
        boot = services["retriva-pg-bootstrap"]["environment"]
        self.assertIn("RETRIVA_PG_CORE_PASSWORD", boot)
        self.assertNotIn("CRM_PG_APPLICATION_PASSWORD", boot)
        # The CRM bootstrap provisions the extension roles only.
        crm_boot = services["retriva-pg-crm-bootstrap"]["environment"]
        for var in ("CRM_PG_APPLICATION_PASSWORD",
                    "CRM_PG_IMPORTER_PASSWORD",
                    "CRM_PG_READONLY_PASSWORD",
                    "CRM_PGADMIN_UI_OPERATOR_PASSWORD"):
            self.assertIn(var, crm_boot)

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_pinned_images_and_pgadmin_bind(self):
        proc = _resolve(profiles=["db"])
        services = _services(proc.stdout)
        self.assertIn("postgres:16.15", str(services["retriva-postgres"]))
        self.assertIn("pgadmin4:9.18", str(services["retriva-pgadmin"]))
        ports = services["retriva-pgadmin"]["ports"]
        self.assertIn("127.0.0.1", str(ports))

    def test_compose_source_has_pg_volumes_and_mandatory_services(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("retriva_pg_data:", text)
        self.assertIn("retriva_pgadmin_data:", text)
        # The PG lifecycle is no longer profile-gated.
        for block in ("retriva-postgres:\n", "retriva-pg-bootstrap:\n",
                      "retriva-pg-migrate:\n"):
            section = re.search(
                rf"  {re.escape(block)}(.*?)(?=\n  \w|\Z)", text, re.S)
            self.assertIsNotNone(section, block)
            self.assertNotIn("profiles:", section.group(1),
                             f"{block} must not be profile-gated")
        # pgAdmin remains the optional operator view.
        self.assertIn('profiles: ["db", "pgadmin"]', text)
        # No `latest` for the platform (pinned versions only).
        pg_block = re.search(
            r"retriva-postgres:\n(.*?)(?=\n  \w|\Z)", text, re.S)
        self.assertNotIn(":latest", pg_block.group(1))


class EnvExampleContract(unittest.TestCase):

    def test_platform_block_with_placeholder_credentials(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("RETRIVA_PG_IMAGE=postgres:16.15-alpine", text)
        self.assertIn("RETRIVA_PGADMIN_IMAGE=dpage/pgadmin4:9.18", text)
        for var in ("RETRIVA_PG_ADMIN_PASSWORD=",
                    "RETRIVA_PG_MIGRATOR_PASSWORD=",
                    "RETRIVA_PG_CORE_PASSWORD=",
                    "CRM_PG_APPLICATION_PASSWORD=",
                    "CRM_PG_IMPORTER_PASSWORD=",
                    "CRM_PG_READONLY_PASSWORD=",
                    "CRM_PGADMIN_UI_OPERATOR_PASSWORD=",
                    "RETRIVA_PGADMIN_UI_PASSWORD="):
            self.assertIn(var, text)
        # Real credentials are never committed: every PostgreSQL /
        # pgAdmin password line in the example must be empty (values
        # are provided per-installation via .env or secret files).
        pg_secret_vars = (
            "RETRIVA_PG_ADMIN_PASSWORD",
            "RETRIVA_PG_MIGRATOR_PASSWORD",
            "RETRIVA_PG_CORE_PASSWORD",
            "CRM_PG_APPLICATION_PASSWORD",
            "CRM_PG_IMPORTER_PASSWORD",
            "CRM_PG_READONLY_PASSWORD",
            "CRM_PGADMIN_UI_OPERATOR_PASSWORD",
            "RETRIVA_PGADMIN_UI_PASSWORD",
            "RETRIVA_MESSAGING_DB_PASSWORD",
        )
        for line in text.splitlines():
            for var in pg_secret_vars:
                if re.match(rf"^{var}=.+$", line):
                    self.fail(f"committed credential value: {line}")
        # File indirection documented for every password.
        for var in ("RETRIVA_PG_ADMIN_PASSWORD_FILE",
                    "RETRIVA_PG_MIGRATOR_PASSWORD_FILE",
                    "RETRIVA_PG_CORE_PASSWORD_FILE",
                    "CRM_PG_APPLICATION_PASSWORD_FILE",
                    "RETRIVA_PGADMIN_UI_PASSWORD_FILE"):
            self.assertIn(f"#{var}=", text)
        # Runtime activation is off by default (reversible phase).
        self.assertIn("CRM_PG_ENABLED=false", text)

    def test_deprecated_crm_pg_compatibility_documented(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        # The CRM_PG_* platform names are deprecated compatibility
        # aliases, commented out and replaced by RETRIVA_PG_*.
        self.assertIn("DEPRECATED compatibility path", text)
        self.assertIn("#CRM_PG_HOST=retriva-postgres", text)
        self.assertIn("RETRIVA_PG_DATABASE=retriva", text)

    def test_messaging_block_targets_shared_database(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn("MESSAGING_DB_NAME", text)
        self.assertNotIn("MESSAGING_DB_IMAGE", text)
        self.assertNotIn("/retriva_messaging", text)
        self.assertNotIn("retriva_messaging@localhost", text)
        self.assertIn("RETRIVA_MESSAGING_DB_USER=retriva_messaging", text)
        self.assertIn("RETRIVA_MESSAGING_DB_PASSWORD=", text)
        self.assertIn("Pro-owned `messaging` schema", text)


class ManageShContract(unittest.TestCase):

    def test_db_commands_present(self):
        text = MANAGE.read_text(encoding="utf-8")
        for cmd in ("db-up)", "db-down)", "db-migrate)", "db-status)",
                    "db-verify)", "db-readiness)", "db-psql)", "db-logs)",
                    "db-messaging-bootstrap)", "db-messaging-migrate)"):
            self.assertIn(cmd, text)

    def test_up_paths_require_platform_credentials(self):
        text = MANAGE.read_text(encoding="utf-8")
        # Core-only `up` requires the platform credential set.
        self.assertIn("_require_pg_env", text)
        self.assertIn("RETRIVA_PG_CORE_PASSWORD", text)
        # Pro path additionally requires the CRM extension credentials.
        self.assertIn("_require_crm_env", text)
        self.assertIn("CRM_PG_APPLICATION_PASSWORD", text)
        # The Core-only up path must NOT require CRM credentials.
        up_block = re.search(r"  up\)\n(.*?)\n    ;;", text, re.S)
        self.assertIsNotNone(up_block)
        self.assertIn("_require_pg_env", up_block.group(1))
        self.assertNotIn("_require_crm_env", up_block.group(1))

    def test_up_starts_the_pg_lifecycle(self):
        text = MANAGE.read_text(encoding="utf-8")
        up_block = re.search(r"  up\)\n(.*?)\n    ;;", text, re.S)
        self.assertIsNotNone(up_block)
        for svc in PLATFORM_SERVICES:
            self.assertIn(svc, up_block.group(1))

    def test_no_dangling_db_env_check(self):
        text = MANAGE.read_text(encoding="utf-8")
        self.assertNotIn("_require_db_env", text)

    def test_no_messaging_db_service_references(self):
        text = MANAGE.read_text(encoding="utf-8")
        self.assertNotIn("retriva-messaging-db)", text)


class PgAdminServerRegistration(unittest.TestCase):

    def test_servers_json_registers_internal_host_without_credentials(self):
        text = PGADMIN_SERVERS.read_text(encoding="utf-8")
        self.assertIn('"Host": "retriva-postgres"', text)
        self.assertIn('"Port": 5432', text)
        self.assertIn('"Username": "retriva_pgadmin_operator"', text)
        # No credentials in the pre-registered server definition.
        self.assertNotIn("Password", text)


if __name__ == "__main__":
    unittest.main()
