#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Engine first: https://docs.docker.com/engine/install/"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 plugin is not available. Install docker-compose-plugin."
  exit 1
fi

./scripts/manage.sh init
./scripts/manage.sh check

echo
cat <<'EOF'
Next steps:
1. Edit .env
   - Set OPENROUTER_API_KEY
   - Check RETRIVA_*_DIR paths
   - Set MEDIAWIKI_* only if testing connector
2. Run:
   ./scripts/manage.sh build
   ./scripts/manage.sh up
3. Open:
   http://localhost:5173
EOF
