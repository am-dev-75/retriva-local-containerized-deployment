#!/usr/bin/env bash
# Static, non-live validation for Spec 037 safe defaults.
#
# Renders the committed Compose configuration with a minimal synthetic
# env file (created in a temp dir, never committed) and asserts:
#   - CRM_CREDITSAFE_* safe defaults on all three pro extension hosts;
#   - no credential values are present;
#   - the acceptance harness is not wired into Compose/startup.
#
# It never starts containers, never reads real credentials, and never
# contacts Creditsafe.
set -euo pipefail
cd "$(dirname "$0")/.."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

gen() { python3 -c 'import secrets; print(secrets.token_urlsafe(12))'; }

cat > "$TMP/env" <<EOF
COMPOSE_PROJECT_NAME=creditsafe-defaults-check
RETRIVA_CORE_DIR=../retriva-core
RETRIVA_CORE_CONTEXT=..
RETRIVA_CORE_DOCKERFILE=retriva-core/Dockerfile
RETRIVA_CORE_BUILD_TARGET=pro
RETRIVA_EXTENSIONS=retriva_crm_assistant
RETRIVA_PG_ADMIN_USER=retriva_admin
RETRIVA_PG_ADMIN_PASSWORD=$(gen)
RETRIVA_PG_MIGRATOR_PASSWORD=$(gen)
RETRIVA_PG_CORE_PASSWORD=$(gen)
RETRIVA_PG_MONITOR_USER=retriva_monitor
RETRIVA_PG_MONITOR_PASSWORD=$(gen)
CRM_PG_ENABLED=true
CRM_PG_APPLICATION_PASSWORD=$(gen)
CRM_PG_IMPORTER_PASSWORD=$(gen)
CRM_PG_READONLY_PASSWORD=$(gen)
CRM_PGADMIN_UI_OPERATOR_PASSWORD=$(gen)
RETRIVA_PGADMIN_UI_PASSWORD=$(gen)
REDIS_BROKER_PASSWORD=$(gen)
REDIS_RESULTS_PASSWORD=$(gen)
REDIS_MONITOR_PASSWORD=$(gen)
REDIS_HEALTH_PASSWORD=$(gen)
EOF
chmod 600 "$TMP/env"

ENV_FILE="$TMP/env" docker compose -f docker-compose.yml \
    config --format json > "$TMP/resolved.json" 2>/dev/null

if grep -q "spec037_acceptance_harness" docker-compose.yml; then
    echo "CREDITSAFE DEFAULTS CHECK: FAIL (acceptance harness wired into Compose)" >&2
    exit 1
fi

python3 - "$TMP/resolved.json" <<'PY'
import json
import sys

EXPECTED = {
    "CRM_CREDITSAFE_ENABLED": "false",
    "CRM_CREDITSAFE_ENVIRONMENT": "sandbox",
    "CRM_CREDITSAFE_USERNAME": "",
    "CRM_CREDITSAFE_PASSWORD": "",
    "CRM_CREDITSAFE_BUDGET_COMPANY_SEARCH": "0",
    "CRM_CREDITSAFE_BUDGET_CREDIT_REPORT": "0",
    "CRM_CREDITSAFE_FRESHNESS_DAYS": "90",
    "CRM_CREDITSAFE_ALLOW_NAME_ONLY_SEARCH": "false",
}
HOSTS = ("retriva-ingestion", "retriva-core", "retriva-worker")

config = json.load(open(sys.argv[1]))
failures = []
for svc in HOSTS:
    env = config["services"][svc].get("environment", {})
    for key, want in EXPECTED.items():
        got = str(env.get(key, "<missing>"))
        if got != want:
            failures.append(f"{svc}:{key}={got!r} (expected {want!r})")
    if env.get("CRM_CREDITSAFE_USERNAME") or env.get("CRM_CREDITSAFE_PASSWORD"):
        failures.append(f"{svc}: credential values present")

if failures:
    print("CREDITSAFE DEFAULTS CHECK: FAIL")
    for item in failures:
        print(" -", item)
    sys.exit(1)

print("CREDITSAFE DEFAULTS CHECK: PASS "
      "(disabled, sandbox, zero budgets, 90d, name-only off, "
      "no credentials on ingestion/core/worker)")
PY
