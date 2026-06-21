#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
export ENV_FILE="${ENV_FILE:-.env}"

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

case "$COMMAND" in
  init)
    if [[ ! -f .env ]]; then
      cp .env.example .env
      echo "Created .env from .env.example. Edit OPENAI_PROVIDER_API_KEY and repository paths before starting."
    else
      echo ".env already exists; leaving it unchanged."
    fi
    mkdir -p data/qdrant data/core data/gateway data/connectors/mediawiki logs config
    ;;

  check)
    require_env
    docker --version
    docker compose version
    echo "Checking repository paths from .env..."
    source .env || true
    for var in RETRIVA_CORE_DIR RETRIVA_GATEWAY_DIR RETRIVA_WEBUI_DIR RETRIVA_MEDIAWIKI_CONNECTOR_DIR; do
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
    if [[ ${#EXCLUDED_SERVICES[@]} -gt 0 ]]; then
      SERVICES=$(compose config --services)
      for ex in "${EXCLUDED_SERVICES[@]}"; do
        SERVICES=$(echo "$SERVICES" | grep -v "^${ex}$" || true)
      done
      if [[ -z "$SERVICES" ]]; then
        echo "No services to build after exclusions."
        exit 0
      fi
      SERVICES=$(echo "$SERVICES" | tr '\n' ' ')
      compose build $SERVICES
    else
      compose build
    fi
    ;;

  up)
    require_env
    SERVICES="qdrant tika whisper retriva-ingestion retriva-core retriva-gateway retriva-webui"
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

  down)
    require_env
    compose down
    ;;

  restart)
    require_env
    compose restart ${2:-}
    ;;

  ps)
    require_env
    compose ps
    ;;

  logs)
    require_env
    shift || true
    compose logs -f --tail=200 "$@"
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
    compose --profile connectors run --rm retriva-mediawiki-connector bash
    ;;

  connector-validate)
    require_env
    compose --profile connectors run --rm retriva-mediawiki-connector retriva-mediawiki-connector validate --config /app/config/mediawiki.yaml
    ;;

  connector-sync)
    require_env
    compose --profile connectors run --rm retriva-mediawiki-connector retriva-mediawiki-connector sync --config /app/config/mediawiki.yaml
    ;;

  delete-containers|clean)
    require_env
    compose down --remove-orphans
    ;;

  delete-volumes|purge)
    require_env
    echo "This will remove containers and named volumes for $PROJECT_NAME. Press Ctrl+C to abort, Enter to continue."
    read -r _
    compose down --remove-orphans --volumes
    ;;

  help|*)
    cat <<'EOF'
Usage: ./scripts/manage.sh [--exclude <service>] <command>

Options:
  --exclude <service> Exclude a specific service (can be used multiple times)

Commands:
  init                Create .env and local folders
  check               Check Docker/Compose and repository paths
  build               Build local Retriva images
  up                  Start qdrant, tika, core, gateway, webui
  up-with-connectors  Start all services including connector profile
  down                Stop services
  restart [service]   Restart all or one service
  ps                  Show status
  logs [service]      Follow logs
  health              Basic health checks
  connector-shell     Open shell in MediaWiki connector container
  connector-validate  Run connector validate command
  connector-sync      Run connector sync command
  delete-containers   Stop and remove containers (alias for clean)
  delete-volumes      Stop and remove containers and volumes (alias for purge)
  clean               Stop and remove containers, keep volumes
  purge               Stop and remove containers and volumes
EOF
    ;;
esac
