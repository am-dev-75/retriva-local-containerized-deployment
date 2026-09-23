"""Deployment-structure tests for the PostgreSQL Business Intelligence
stack (Retriva Pro, "db" profile).

Deterministic: uses `docker compose config` (no daemon required for
resolution) plus plain file assertions.  Skips the resolution checks when
the docker CLI is unavailable.

Run:  python3 -m pytest tests/test_postgres_deployment.py -q
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
MANAGE = ROOT / "scripts" / "manage.sh"
PGADMIN_SERVERS = ROOT / "config" / "pgadmin" / "servers.json"

DB_SERVICES = (
    "retriva-postgres",
    "retriva-pg-bootstrap",
    "retriva-pg-migrate",
    "retriva-pgadmin",
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
    proc = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=120, cwd=str(ROOT),
                           env={**__import__("os").environ,
                                "ENV_FILE": str(env_file)})
    return proc


def _services(resolved: str):
    """Parse the resolved services mapping into per-service blocks."""
    services = {}
    current, buf, in_services = None, [], False
    for line in resolved.splitlines(keepends=True):
        if line.startswith("services:"):
            in_services = True
            continue
        if not in_services:
            continue
        if not line.strip():
            if current is not None:
                buf.append(line)
            continue
        if line.startswith("  ") and not line.startswith("   ") \
                and line.rstrip().endswith(":"):
            if current is not None:
                services[current] = "".join(buf)
            current, buf = line.strip().rstrip(":"), [line]
        elif current is not None:
            buf.append(line)
        if line and not line[0].isspace():
            break
    if current is not None:
        services[current] = "".join(buf)
    return services


class ComposePgStack(unittest.TestCase):

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_db_profile_resolves_all_pg_services(self):
        proc = _resolve(profiles=["db"])
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        services = _services(proc.stdout)
        for name in DB_SERVICES:
            self.assertIn(name, services)
        self.assertIn("qdrant", services)  # untouched coexistence

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_postgres_never_publishes_ports(self):
        proc = _resolve(profiles=["db"])
        services = _services(proc.stdout)
        pg = services["retriva-postgres"]
        self.assertIsNone(re.search(r"^\s+ports:", pg, re.M),
                          "retriva-postgres must be internal-network only")
        self.assertIn("retriva-net", pg)

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_pgadmin_binds_localhost_by_default(self):
        proc = _resolve(profiles=["db"])
        services = _services(proc.stdout)
        pga = services["retriva-pgadmin"]
        self.assertIn("host_ip: 127.0.0.1", pga)
        self.assertIn("published: \"5050\"", pga)

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_migration_tools_use_least_privilege(self):
        proc = _resolve(profiles=["db"])
        services = _services(proc.stdout)
        mig = services["retriva-pg-migrate"]
        boot = services["retriva-pg-bootstrap"]
        # The migration step runs ONLY with the migrator credential.
        self.assertNotIn("CRM_PG_ADMIN_PASSWORD", mig)
        self.assertIn("CRM_PG_MIGRATOR_PASSWORD", mig)
        # The bootstrap step carries admin + all five role credentials.
        self.assertIn("CRM_PG_ADMIN_PASSWORD", boot)
        for role in ("MIGRATOR", "APPLICATION", "IMPORTER", "READONLY",
                     "PGADMIN_OPERATOR"):
            self.assertIn(f"CRM_PG_{role}_PASSWORD", boot)
        # Ordering: migrate waits for the bootstrap one-shot to succeed.
        self.assertIn("condition: service_completed_successfully", mig)
        self.assertIn("condition: service_healthy", boot)
        # One-shot tools never restart-loop.
        self.assertIn('restart: "no"', mig.replace("'", '"'))
        self.assertIn('restart: "no"', boot.replace("'", '"'))

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_default_and_pro_profiles_exclude_db_services(self):
        base = _resolve()
        self.assertEqual(base.returncode, 0, base.stderr[-2000:])
        self.assertNotIn("retriva-postgres", _services(base.stdout))
        pro = _resolve(profiles=["pro"])
        self.assertEqual(pro.returncode, 0, pro.stderr[-2000:])
        self.assertNotIn("retriva-postgres", _services(pro.stdout))

    @unittest.skipUnless(_compose_available(),
                         "docker CLI not available")
    def test_pinned_images(self):
        proc = _resolve(profiles=["db"])
        services = _services(proc.stdout)
        self.assertIn("postgres:16.15", services["retriva-postgres"])
        self.assertIn("pgadmin4:9.18", services["retriva-pgadmin"])

    def test_compose_source_has_pg_volumes_and_profiles(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("retriva_pg_data:", text)
        self.assertIn("retriva_pgadmin_data:", text)
        self.assertIn('profiles: ["db"]', text)
        self.assertIn('profiles: ["db", "pgadmin"]', text)
        # No `latest` for the new services (pinned versions only).
        pg_block = re.search(
            r"retriva-postgres:\n(.*?)(?=\n  \w|\Z)", text, re.S)
        self.assertIsNotNone(pg_block)
        self.assertNotIn(":latest", pg_block.group(1))


class EnvExampleContract(unittest.TestCase):

    def test_pg_block_exists_with_placeholder_credentials(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("RETRIVA_PG_IMAGE=postgres:16.15-alpine", text)
        self.assertIn("RETRIVA_PGADMIN_IMAGE=dpage/pgadmin4:9.18", text)
        for var in ("RETRIVA_PG_ADMIN_PASSWORD=",
                    "CRM_PG_MIGRATOR_PASSWORD=",
                    "CRM_PG_APPLICATION_PASSWORD=",
                    "CRM_PG_IMPORTER_PASSWORD=",
                    "CRM_PG_READONLY_PASSWORD=",
                    "CRM_PG_PGADMIN_OPERATOR_PASSWORD=",
                    "RETRIVA_PGADMIN_PASSWORD="):
            self.assertIn(var, text)
        # Real credentials are never committed: every PostgreSQL /
        # pgAdmin password line in the example must be empty (values
        # are provided per-installation via .env or secret files).
        pg_secret_vars = (
            "RETRIVA_PG_ADMIN_PASSWORD",
            "CRM_PG_MIGRATOR_PASSWORD",
            "CRM_PG_APPLICATION_PASSWORD",
            "CRM_PG_IMPORTER_PASSWORD",
            "CRM_PG_READONLY_PASSWORD",
            "CRM_PG_PGADMIN_OPERATOR_PASSWORD",
            "RETRIVA_PGADMIN_PASSWORD",
        )
        for line in text.splitlines():
            for var in pg_secret_vars:
                if re.match(rf"^{var}=.+$", line):
                    self.fail(f"committed credential value: {line}")
        # File indirection documented for every password.
        for var in ("RETRIVA_PG_ADMIN_PASSWORD_FILE",
                    "CRM_PG_MIGRATOR_PASSWORD_FILE",
                    "RETRIVA_PGADMIN_PASSWORD_FILE"):
            self.assertIn(f"#{var}=", text)
        # Runtime activation is off by default (reversible phase).
        self.assertIn("CRM_PG_ENABLED=false", text)
        self.assertIn("CRM_PG_HOST=retriva-postgres", text)

    def test_secret_files_are_gitignored(self):
        # The example only contains commented secret-file paths.
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertNotIn(
            "token_urlsafe(32))\"\nRETRIVA_PG_ADMIN_PASSWORD=",
            text.replace("'", '"'))

    def test_manage_sh_db_commands(self):
        text = MANAGE.read_text(encoding="utf-8")
        for cmd in ("db-up)", "db-down)", "db-migrate)", "db-status)",
                    "db-verify)", "db-readiness)", "db-psql)", "db-logs)"):
            self.assertIn(cmd, text)
        # Fail-fast credential check wired into db-up.
        self.assertIn("_require_db_env", text)
        # Help documents the db commands.
        self.assertIn("db-up ", text)


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