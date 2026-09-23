#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
export ENV_FILE="${ENV_FILE:-.env}"

# Automatically detect host timezone and export it for docker-compose
if [[ -f /etc/timezone ]]; then
  export TZ=$(cat /etc/timezone)
elif [[ -L /etc/localtime ]]; then
  export TZ=$(readlink /etc/localtime | sed "s|.*zoneinfo/||")
else
  export TZ="UTC"
fi

PROJECT_NAME="${PROJECT_NAME:-retriva-local}"
if [[ -f "$ENV_FILE" ]]; then
  ENV_PROJECT_NAME=$(grep -E '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" | cut -d '=' -f 2- || true)
  if [[ -n "$ENV_PROJECT_NAME" ]]; then
    PROJECT_NAME="$ENV_PROJECT_NAME"
  fi
fi

EXCLUDED_SERVICES=()
ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --exclude)
      if [[ -z "${2:-}" ]]; then
        echo "ERROR: --exclude requires a service name." >&2
        exit 1
      fi
      EXCLUDED_SERVICES+=("$2")
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

set -- "${ARGS[@]:-}"
COMMAND="${1:-help}"
if [[ ${#ARGS[@]} -gt 0 ]]; then
  shift
fi

compose() {
  docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

require_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found. Run: cp .env.example .env and edit it." >&2
    exit 1
  fi
}

# Auto-exclude connectors that are disabled in the .env file.
# Each connector has an ENABLED flag (on/off, default: off).
# When disabled, the connector service is added to EXCLUDED_SERVICES
# so it is not started by up-pro/up-with-connectors/build.
_auto_exclude_disabled_connectors() {
  if [[ ! -f "$ENV_FILE" ]]; then
    return
  fi

  # MediaWiki Connector #1
  local mw_enabled
  mw_enabled=$(grep -E '^MEDIAWIKI_CONNECTOR_ENABLED=' "$ENV_FILE" 2>/dev/null | cut -d '=' -f 2- | tr -d '[:space:]' || true)
  if [[ "$mw_enabled" != "on" ]]; then
    EXCLUDED_SERVICES+=("retriva-mediawiki-connector")
  fi

  # MediaWiki Connector #2 (only if the service is uncommented in docker-compose)
  local mw2_enabled
  mw2_enabled=$(grep -E '^MEDIAWIKI_CONNECTOR_2_ENABLED=' "$ENV_FILE" 2>/dev/null | cut -d '=' -f 2- | tr -d '[:space:]' || true)
  if [[ "$mw2_enabled" != "on" ]]; then
    EXCLUDED_SERVICES+=("retriva-mediawiki-connector-2")
  fi

  # Email Agent Connector
  local email_enabled
  email_enabled=$(grep -E '^EMAIL_AGENT_CONNECTOR_ENABLED=' "$ENV_FILE" 2>/dev/null | cut -d '=' -f 2- | tr -d '[:space:]' || true)
  if [[ "$email_enabled" != "on" ]]; then
    EXCLUDED_SERVICES+=("retriva-email-agent-connector")
  fi

  # Messaging Extension (retriva-messaging, apprise-api, retriva-messaging-db)
  local msg_enabled
  msg_enabled=$(grep -E '^RETRIVA_MESSAGING_ENABLED=' "$ENV_FILE" 2>/dev/null | cut -d '=' -f 2- | tr -d '[:space:]' || true)
  if [[ "$msg_enabled" != "on" ]]; then
    EXCLUDED_SERVICES+=("retriva-messaging")
    EXCLUDED_SERVICES+=("apprise-api")
    EXCLUDED_SERVICES+=("retriva-messaging-db")
  fi
}

# PostgreSQL Business Intelligence Database ("db" profile): fail fast
# when the credentials the stack requires are not configured (value or
# mounted secret file) in the .env file.
_require_db_env() {
  require_env
  local missing=()
  local value file_var file_value
  local vars=(
    RETRIVA_PG_ADMIN_PASSWORD CRM_PG_MIGRATOR_PASSWORD
    CRM_PG_APPLICATION_PASSWORD CRM_PG_IMPORTER_PASSWORD
    CRM_PG_READONLY_PASSWORD CRM_PGADMIN_UI_OPERATOR_PASSWORD
    RETRIVA_PGADMIN_UI_PASSWORD
  )
  for var in "${vars[@]}"; do
    value=$(grep -E "^${var}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d '=' -f 2- | tr -d '[:space:]' || true)
    file_var=""
    case "$var" in
      RETRIVA_PG_ADMIN_PASSWORD) file_var=RETRIVA_PG_ADMIN_PASSWORD_FILE ;;
      CRM_PG_MIGRATOR_PASSWORD) file_var=CRM_PG_MIGRATOR_PASSWORD_FILE ;;
      CRM_PG_APPLICATION_PASSWORD) file_var=CRM_PG_APPLICATION_PASSWORD_FILE ;;
      CRM_PG_IMPORTER_PASSWORD) file_var=CRM_PG_IMPORTER_PASSWORD_FILE ;;
      CRM_PG_READONLY_PASSWORD) file_var=CRM_PG_READONLY_PASSWORD_FILE ;;
      CRM_PGADMIN_UI_OPERATOR_PASSWORD) file_var=CRM_PGADMIN_UI_OPERATOR_PASSWORD_FILE ;;
      RETRIVA_PGADMIN_UI_PASSWORD) file_var=RETRIVA_PGADMIN_UI_PASSWORD_FILE ;;
    esac
    file_value=""
    if [[ -n "$file_var" ]]; then
      file_value=$(grep -E "^${file_var}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d '=' -f 2- | tr -d '[:space:]' || true)
    fi
    if [[ -z "$value" && -z "$file_value" ]]; then
      missing+=("$var")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: the following variables must be set (value or _FILE) in $ENV_FILE before starting the database:" >&2
    printf '       %s\n' "${missing[@]}" >&2
    echo "Generate local secrets with: python -c 'import secrets; print(secrets.token_urlsafe(32))'" >&2
    exit 1
  fi
}

# Call auto-exclusion before processing commands that start/build services.
_auto_exclude_disabled_connectors

case "$COMMAND" in
  init)
    if [[ ! -f .env ]]; then
      cp .env.example .env
      echo "Created .env from .env.example. Edit OPENAI_PROVIDER_API_KEY and repository paths before starting."
    else
      echo ".env already exists; leaving it unchanged."
    fi
    mkdir -p data/qdrant data/core data/gateway data/connectors/mediawiki data/connectors/email data/messaging logs config
    ;;

  check)
    require_env
    docker --version
    docker compose version
    echo "Checking repository paths from $ENV_FILE..."
    source "$ENV_FILE" || true
    for var in RETRIVA_CORE_DIR RETRIVA_GATEWAY_DIR RETRIVA_WEBUI_DIR RETRIVA_MEDIAWIKI_CONNECTOR_DIR RETRIVA_EMAIL_AGENT_CONNECTOR_DIR RETRIVA_MESSAGING_DIR; do
      val="${!var:-}"
      if [[ -n "$val" && -d "$val" ]]; then
        echo "OK: $var=$val"
      else
        echo "WARN: $var=$val does not exist or is not set"
      fi
    done
    ;;

  build)
    require_env
    SERVICES="qdrant redis tika whisper retriva-searxng retriva-ingestion retriva-worker retriva-core retriva-gateway retriva-webui"
    if [[ ${#EXCLUDED_SERVICES[@]} -gt 0 ]]; then
      for ex in "${EXCLUDED_SERVICES[@]}"; do
        SERVICES=$(echo "$SERVICES" | tr ' ' '\n' | grep -v "^${ex}$" | tr '\n' ' ' || true)
      done
      if [[ -z "$SERVICES" ]]; then
        echo "No services to build after exclusions."
        exit 0
      fi
    fi
    compose build $SERVICES
    ;;

  build-pro)
    require_env
    if [[ ${#EXCLUDED_SERVICES[@]} -gt 0 ]]; then
      SERVICES=$(compose --profile pro config --services)
      for ex in "${EXCLUDED_SERVICES[@]}"; do
        SERVICES=$(echo "$SERVICES" | grep -v "^${ex}$" || true)
      done
      SERVICES=$(echo "$SERVICES" | tr '\n' ' ')
      compose --profile pro build $SERVICES
    else
      compose --profile pro build
    fi
    ;;

  up)
    require_env
    SERVICES="qdrant redis tika whisper retriva-searxng retriva-ingestion retriva-worker retriva-core retriva-gateway retriva-webui"
    if [[ ${#EXCLUDED_SERVICES[@]} -gt 0 ]]; then
      for ex in "${EXCLUDED_SERVICES[@]}"; do
        SERVICES=$(echo "$SERVICES" | tr ' ' '\n' | grep -v "^${ex}$" | tr '\n' ' ' || true)
      done
    fi
    compose up -d $SERVICES
    ;;

  up-with-connectors)
    require_env
    if [[ ${#EXCLUDED_SERVICES[@]} -gt 0 ]]; then
      SERVICES=$(compose --profile connectors config --services)
      for ex in "${EXCLUDED_SERVICES[@]}"; do
        SERVICES=$(echo "$SERVICES" | grep -v "^${ex}$" || true)
      done
      SERVICES=$(echo "$SERVICES" | tr '\n' ' ')
      compose --profile connectors up -d $SERVICES
    else
      compose --profile connectors up -d
    fi
    ;;

  up-pro)
    require_env
    if [[ ${#EXCLUDED_SERVICES[@]} -gt 0 ]]; then
      SERVICES=$(compose --profile pro config --services)
      for ex in "${EXCLUDED_SERVICES[@]}"; do
        SERVICES=$(echo "$SERVICES" | grep -v "^${ex}$" || true)
      done
      SERVICES=$(echo "$SERVICES" | tr '\n' ' ')
      compose --profile pro up -d $SERVICES
    else
      compose --profile pro up -d
    fi
    ;;

  down)
    require_env
    compose --profile pro down
    ;;

  restart)
    require_env
    compose restart "$@"
    ;;

  rebuild)
    require_env
    if [[ $# -eq 0 ]]; then
      echo "ERROR: rebuild requires at least one service name." >&2
      echo "Usage: ./scripts/manage.sh rebuild <service> [service ...]" >&2
      exit 1
    fi
    compose build "$@"
    for svc in "$@"; do
      compose rm -f -s "$svc" 2>/dev/null || true
    done
    compose up -d --no-deps "$@"
    ;;

  ps)
    require_env
    compose ps
    ;;

  logs)
    require_env
    FOLLOW=true
    if [[ "${1:-}" == "--no-follow" ]]; then
      FOLLOW=false
      shift
    fi
    if [[ "$FOLLOW" == "true" ]]; then
      compose --profile pro logs -f --tail=200 "$@"
    else
      compose --profile pro logs --tail=200 "$@"
    fi
    ;;

  health)
    require_env
    echo "Qdrant:  http://localhost:${QDRANT_HTTP_PORT:-6333}/dashboard"
    curl -fsS "http://localhost:${QDRANT_HTTP_PORT:-6333}/" >/dev/null && echo "OK qdrant" || echo "FAIL qdrant"
    curl -fsS "http://localhost:${TIKA_PORT:-9998}/tika" >/dev/null && echo "OK tika" || echo "FAIL tika"
    curl -fsS "http://localhost:${GATEWAY_PORT:-8002}/gateway/health" && echo "OK gateway" || echo "WARN gateway health endpoint failed"
    echo "WebUI:   http://localhost:${WEBUI_PORT:-5173}"
    ;;

  connector-shell)
    require_env
    compose --profile pro run --rm retriva-mediawiki-connector bash
    ;;

  db-up)
    _require_db_env
    compose --profile db up -d retriva-postgres retriva-pg-bootstrap retriva-pg-migrate retriva-pgadmin
    echo
    echo "PostgreSQL stack started. pgAdmin: http://${RETRIVA_PGADMIN_BIND_ADDR:-127.0.0.1}:${RETRIVA_PGADMIN_PORT:-5050}"
    echo "Connect pgAdmin to host 'retriva-postgres' (internal network, port 5432)."
    echo "Activate the runtime store by setting CRM_PG_ENABLED=true and restarting the Pro services."
    ;;

  db-down)
    require_env
    compose --profile db --profile pgadmin down
    ;;

  db-migrate)
    require_env
    compose --profile db run --rm retriva-pg-migrate
    ;;

  db-status)
    require_env
    compose --profile db run --rm retriva-pg-migrate python -m retriva_crm_assistant.postgres.migrate status
    ;;

  db-verify)
    require_env
    compose --profile db run --rm retriva-pg-migrate python -m retriva_crm_assistant.postgres.migrate verify
    ;;

  db-readiness)
    require_env
    compose --profile db run --rm retriva-pg-migrate python -m retriva_crm_assistant.postgres.migrate readiness
    ;;

  db-psql)
    require_env
    docker exec -it retriva-postgres psql \
      -U "$(grep -E '^RETRIVA_PG_ADMIN_USER=' "$ENV_FILE" | cut -d '=' -f 2- || echo retriva_admin)" \
      -d "$(grep -E '^RETRIVA_PG_DATABASE=' "$ENV_FILE" | cut -d '=' -f 2- || echo retriva)"
    ;;

  db-logs)
    require_env
    if [[ $# -eq 0 ]]; then
      docker logs retriva-postgres
    else
      docker logs "$@"
    fi
    ;;

  connector-validate)
    require_env
    compose --profile pro run --rm retriva-mediawiki-connector validate --config /app/config/mediawiki.yaml
    ;;

  connector-sync)
    require_env
    compose --profile pro run --rm retriva-mediawiki-connector sync --config /app/config/mediawiki.yaml
    ;;

  email-shell)
    require_env
    compose --profile pro run --rm retriva-email-agent-connector bash
    ;;

  email-validate)
    require_env
    compose --profile pro run --rm retriva-email-agent-connector validate --config /app/config/email-agent.yaml
    ;;

  email-run)
    require_env
    compose --profile pro up -d --no-deps retriva-email-agent-connector
    ;;

  pro-shell)
    require_env
    compose --profile pro run --rm retriva-mediawiki-connector bash
    ;;

  pro-validate)
    require_env
    compose --profile pro run --rm retriva-mediawiki-connector validate --config /app/config/mediawiki.yaml
    ;;

  pro-sync)
    require_env
    compose --profile pro run --rm retriva-mediawiki-connector sync --config /app/config/mediawiki.yaml
    ;;

  delete-containers|clean)
    require_env
    compose --profile pro down --remove-orphans
    ;;

  delete-volumes|purge)
    require_env
    echo "This will remove containers and named volumes for $PROJECT_NAME. Press Ctrl+C to abort, Enter to continue."
    read -r _
    compose --profile pro down --remove-orphans --volumes
    ;;

  help|*)
    cat <<'EOF'
Usage: ./scripts/manage.sh [--exclude <service>] <command>

Options:
  --exclude <service> Exclude a specific service (can be used multiple times)

Connector auto-exclusion:
  Connectors with ENABLED=off (the default) in the .env file are
  automatically excluded from up-pro, up-with-connectors, and build.
  Set ENABLED=on to enable a connector/extension on startup:
    MEDIAWIKI_CONNECTOR_ENABLED=on
    MEDIAWIKI_CONNECTOR_2_ENABLED=on
    EMAIL_AGENT_CONNECTOR_ENABLED=on
    RETRIVA_MESSAGING_ENABLED=on

Commands:
  init                Create .env and local folders
  check               Check Docker/Compose and repository paths
  build               Build local Retriva images
  build-pro           Build core images plus all Retriva Pro profile services
  up                  Start qdrant, tika, core, gateway, webui
  up-with-connectors  Start all services including connector profile (alias for up-pro)
  up-pro              Start all services including Retriva Pro extensions
  down                Stop services
  restart [service]   Restart all or one service
  rebuild <svc> ...   Rebuild and recreate specific services (no deps)
  ps                  Show status
  logs [--no-follow] [service]
                      Show logs (follow by default)
  health              Basic health checks
  connector-shell     Open shell in MediaWiki connector container (alias: pro-shell)
  connector-validate  Run connector validate command (alias: pro-validate)
  connector-sync      Run connector sync command (alias: pro-sync)
  db-up               Start the PostgreSQL Business Intelligence stack
                      (postgres + role bootstrap + migrations + pgAdmin; "db" profile)
  db-down             Stop the PostgreSQL stack (keeps volumes)
  db-migrate          Apply pending PostgreSQL migrations (controlled step)
  db-status           Show PostgreSQL migration ledger and pending state
  db-verify           Verify PostgreSQL RLS/role invariants
  db-readiness        PostgreSQL readiness report (no credentials)
  db-psql             Open psql inside retriva-postgres (local trust socket)
  db-logs             Show PostgreSQL container logs (docker logs args)
  email-shell         Open shell in Email Agent connector container
  email-validate      Run Email Agent connector validate command
  email-run            Start Email Agent connector (SMTP server)
  pro-shell           Open shell in MediaWiki connector container
  pro-validate        Run connector validate command
  pro-sync            Run connector sync command
  delete-containers   Stop and remove containers (alias for clean)
  delete-volumes      Stop and remove containers and volumes (alias for purge)
  clean               Stop and remove containers, keep volumes
  purge               Stop and remove containers and volumes
EOF
    ;;
esac
