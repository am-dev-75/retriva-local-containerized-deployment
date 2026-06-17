# Retriva Local Containerized Deployment

This folder provides a local Docker Compose deployment for development/testing.

It runs:

- Qdrant
- Apache Tika
- Retriva Core
- Retriva Gateway
- Retriva WebUI
- Retriva connectors profile, currently MediaWiki connector

An existing, remote service is used as the OpenAI-compatible LLM provider, so no local LLM server is deployed.

## Expected repository layout

Place this folder next to your Retriva repositories:

```text
workspace/
├── retriva-local-containerized-deployment/
├── retriva-core/
├── retriva-gateway/
├── retriva-webui/
└── retriva-mediawiki-connector/
```

If your layout differs, edit `.env` after initialization.

## Quick start on Linux

```bash
cd retriva-local-containerized-deployment
./scripts/bootstrap-linux.sh
nano .env
./scripts/manage.sh build
./scripts/manage.sh up
./scripts/manage.sh health
```

Open:

```text
http://localhost:5173
```

Useful service URLs:

```text
WebUI:          http://localhost:5173
Gateway:        http://localhost:8002
Core:           http://localhost:8001
Qdrant UI:      http://localhost:6333/dashboard
Tika:           http://localhost:9998
```

## Run connector commands

The connector service is under the `connectors` Compose profile. It does not run by default.

```bash
./scripts/manage.sh up-with-connectors
./scripts/manage.sh connector-validate
./scripts/manage.sh connector-sync
```

If the connector CLI is not yet implemented, use:

```bash
./scripts/manage.sh connector-shell
```

## Common operations

```bash
./scripts/manage.sh ps
./scripts/manage.sh logs
./scripts/manage.sh logs retriva-core
./scripts/manage.sh restart retriva-gateway
./scripts/manage.sh down
./scripts/manage.sh purge
```

## Notes

- This is a local development deployment.
- Ports are bound to localhost where convenient, but Qdrant/Core/Gateway/WebUI are exposed on host ports for debugging.
- Do not use this unchanged in production.
- The Compose file intentionally uses `build:` for Retriva services, so it expects local repositories with Dockerfiles.

## Licensing

This project, including all source code, agentic specifications, and documentation, is licensed under the Apache License 2.0. See the LICENSE file for details.