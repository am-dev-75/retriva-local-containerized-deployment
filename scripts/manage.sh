#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
ENV_FILE="${ENV_FILE:-.env}"
PROJECT_NAME="${PROJECT_NAME:-retriva-local}"

compose() {
  docker compose --project-name "$PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

require_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found. Run: cp .env.example .env and edit it." >&2
    exit 1
  fi
}

case "${1:-help}" in
  init)
    if [[ ! -f .env ]]; then
      cp .env.example .env
      echo "Created .env from .env.example. Edit OPENROUTER_API_KEY and repository paths before starting."
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
    compose build
    ;;

  up)
    require_env
    compose up -d qdrant tika retriva-core retriva-gateway retriva-webui
    ;;

  up-with-connectors)
    require_env
    compose --profile connectors up -d
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

  clean)
    require_env
    compose down --remove-orphans
    ;;

  purge)
    require_env
    echo "This will remove containers and named volumes for $PROJECT_NAME. Press Ctrl+C to abort, Enter to continue."
    read -r _
    compose down --remove-orphans --volumes
    ;;

  help|*)
    cat <<'EOF'
Usage: ./scripts/manage.sh <command>

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
  clean               Stop and remove containers, keep volumes
  purge               Stop and remove containers and volumes
EOF
    ;;
esac
