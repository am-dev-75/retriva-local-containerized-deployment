# Retriva Local Containerized Deployment

- [Retriva Local Containerized Deployment](#retriva-local-containerized-deployment)
  - [Retriva Core vs Retriva Pro](#retriva-core-vs-retriva-pro)
  - [Expected repository layout](#expected-repository-layout)
  - [Quick start on Linux](#quick-start-on-linux)
  - [Retriva Pro extensions (`up-pro` vs `up`)](#retriva-pro-extensions-up-pro-vs-up)
  - [Authentication](#authentication)
    - [Enabling Microsoft Entra ID](#enabling-microsoft-entra-id)
  - [Collections Handling](#collections-handling)
    - [With Authentication Disabled](#with-authentication-disabled)
    - [With Microsoft Entra ID](#with-microsoft-entra-id)
  - [Excluding Services](#excluding-services)
  - [Common operations](#common-operations)
  - [Overriding Settings Globally](#overriding-settings-globally)
    - [MediaWiki Connector Settings](#mediawiki-connector-settings)
    - [Multiple MediaWiki Instances](#multiple-mediawiki-instances)
      - [Setup](#setup)
      - [How Tags Work](#how-tags-work)
      - [Managing Individual Connectors](#managing-individual-connectors)
      - [Adding More Wikis](#adding-more-wikis)
  - [Managing Multiple Deployments (Tagging)](#managing-multiple-deployments-tagging)
  - [Notes](#notes)
  - [Licensing](#licensing)

Version: 1.6.0

This folder provides a local Docker Compose deployment for development/testing.

It runs:

- Qdrant
- Apache Tika
- Retriva Ingestion
- Retriva Core
- Retriva Gateway
- Retriva WebUI
- Whisper Server (for WebUI Speech-to-Text)
- Retriva Pro extensions profile, currently the MediaWiki connector

An existing, remote service is used as the OpenAI-compatible LLM provider, so no local LLM server is deployed.

## Retriva Core vs Retriva Pro

**Retriva Core** (`retriva-core`, `retriva-gateway`, `retriva-webui`) is released under the Apache License 2.0. The default `up` command starts only these components plus infrastructure services (Qdrant, Tika, Redis, Whisper).

**Retriva Pro** is the commercial bundle formed by Retriva Core plus proprietary extension containers. Extensions are licensed separately from the core and are **not** covered by the Apache 2.0 license. The first such extension is `retriva-mediawiki-connector`.

Pro extensions are tagged with the `pro` Docker Compose profile. They are only started when you explicitly request the Pro bundle, so a plain `up` never pulls, builds, or runs proprietary code. This keeps the Apache-2.0-licensed deployment self-contained.

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
Ingestion:      http://localhost:8000
Qdrant UI:      http://localhost:6333/dashboard
Tika:           http://localhost:9998
Whisper Server: http://localhost:8100
```

## Retriva Pro extensions (`up-pro` vs `up`)

- **`./scripts/manage.sh up`**: Starts only the default core services (`qdrant`, `tika`, `whisper`, `retriva-ingestion`, `retriva-core`, `retriva-gateway`, `retriva-webui`). It deliberately ignores all Pro extension containers, so the running deployment is purely Apache-2.0-licensed code.
- **`./scripts/manage.sh up-pro`** (alias: `up-with-connectors`): Starts all the default core services **plus** every service tagged with the `pro` Docker Compose profile. This is the Retriva Pro bundle. Each new commercial extension should be added under `profiles: ["pro"]` in `docker-compose.yml`.

Once the Pro services are running, you can execute their specific commands:

```bash
./scripts/manage.sh pro-validate
./scripts/manage.sh pro-sync
```

The `connector-*` commands (`connector-shell`, `connector-validate`, `connector-sync`) are kept as backward-compatible aliases for the `pro-*` commands.

If the connector CLI is not yet implemented, use:

```bash
./scripts/manage.sh pro-shell
```

## Authentication

Authentication is disabled by default for local development:

```env
RETRIVA_AUTH_PROVIDER=none
VITE_RETRIVA_AUTH_PROVIDER=none
```

In this mode the Gateway uses its anonymous `NullAuthProvider`, the WebUI renders without login, and API requests do not include an `Authorization` header.

### Enabling Microsoft Entra ID

Microsoft Entra ID support is provided by the proprietary `retriva-iam-entra` package. The Apache-2.0 Gateway image contains only the generic auth abstraction; it does not include Entra validation code.

To build a local Pro Gateway image with the Entra provider installed, set the following in your `.env` (see the Authentication section of `.env.example` for the full list of Entra variables):

```env
RETRIVA_GATEWAY_CONTEXT=..
RETRIVA_GATEWAY_DOCKERFILE=retriva-local-containerized-deployment/Dockerfile.gateway-pro
RETRIVA_AUTH_PROVIDER=entra
VITE_RETRIVA_AUTH_PROVIDER=entra
```

Then configure the Entra app registration variables documented in `.env.example` and `../retriva-iam-entra/README.md`.

Important notes:

- `VITE_RETRIVA_*` values are embedded into the WebUI at build time. Rebuild `retriva-webui` after changing them.
- Do not commit `.env` files containing tenant-specific values or secrets.
- Do not log access tokens. Gateway logs should contain principal identifiers only.
- If `RETRIVA_AUTH_PROVIDER=entra` but the Pro provider package is not installed, the Gateway intentionally fails closed at startup.
- `RETRIVA_AUTH_EXEMPT_PATHS` accepts either a comma-separated list (`/health,/ready,/capabilities`) or a JSON array.

Troubleshooting:

| Symptom                                               | Likely cause                                  | Fix                                                          |
| ----------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------ |
| Gateway fails with “no matching package is installed” | Pro provider not installed in Gateway image   | Use `Dockerfile.gateway-pro` build path above.               |
| `Invalid token audience`                              | Entra API scope / Application ID URI mismatch | Set `RETRIVA_ENTRA_AUDIENCE` to the access token `aud`.      |
| `Invalid token issuer`                                | Wrong tenant or issuer override               | Check `RETRIVA_ENTRA_TENANT_ID` / `RETRIVA_ENTRA_ISSUER`.    |
| `Token has expired`                                   | Expired access token                          | Sign in again.                                               |
| WebUI redirect mismatch                               | SPA redirect URI not registered               | Add `http://localhost:5173` and production origins in Entra. |
| CORS errors                                           | Gateway CORS origin missing                   | Add WebUI origin to `GATEWAY_CORS_ORIGINS`.                  |
| Changed VITE vars but UI unchanged                    | WebUI image still has old build-time values   | Rebuild `retriva-webui`.                                     |

## Collections Handling

Retriva supports true multi-tenant collection isolation. The backend partitions vector storage, local artifacts, and knowledge base registries per collection.

### With Authentication Disabled

When running locally without authentication (`RETRIVA_AUTH_PROVIDER=none`), the Gateway operates in an anonymous mode where collection access restrictions are not enforced.

1. **Default Collection**: By default, all requests are routed to the collection specified by the `RETRIVA_DEFAULT_COLLECTION` environment variable (defaults to `retriva_chunks`).
2. **Dynamic Selection**: A client (e.g., the WebUI or `curl`) can dynamically switch collections by sending the `X-Retriva-Requested-Collection: <collection_name>` HTTP header. The Gateway trusts this header unconditionally and routes the request to the specified collection. You do not need to register collection names ahead of time in `.env`.

### With Microsoft Entra ID

When Entra ID is enabled (`RETRIVA_AUTH_PROVIDER=entra`), collection access is securely enforced based on the custom claims in the user's JWT access token.

1. **Token Claims**: You must configure Entra ID to include the custom claims that define a user's allowed collections.
2. **Env Configuration**: Map these claims in your `.env` file (if they differ from the defaults):
   ```env
   RETRIVA_ENTRA_COLLECTIONS_CLAIM=retriva_collections
   RETRIVA_ENTRA_DEFAULT_COLLECTION_CLAIM=retriva_default_collection
   ```
3. **Authorization**: When the client requests a collection via the `X-Retriva-Requested-Collection` header, the Gateway verifies that the requested collection is listed in the user's token claims. If omitted, the Gateway falls back to the user's `default_collection` claim, or the global `RETRIVA_DEFAULT_COLLECTION`. Unauthorized requests are rejected with a `403 Forbidden` error.

## Excluding Services

You can optionally exclude specific services from being built or started by passing the `--exclude <service_name>` flag to the `manage.sh` script. This is especially useful if you haven't cloned an optional service (like `retriva-mediawiki-connector`) and want to avoid build or startup errors.

You can use the `--exclude` flag multiple times to exclude multiple services. The flag must be placed before the command.

Example, build all default services except the web UI:
```bash
./scripts/manage.sh --exclude retriva-webui build
```

Example, start all Pro services except the MediaWiki connector:
```bash
./scripts/manage.sh --exclude retriva-mediawiki-connector up-pro
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

## Overriding Settings Globally

You can override any application setting (such as `RETRIVA_DEFAULT_COLLECTION`, port numbers, or API keys) by editing your `.env` file. 

If your containers are already up and running, you do not need to tear them down completely. After modifying `.env`, simply run:

```bash
./scripts/manage.sh up
```

Docker Compose will automatically detect that the environment variables have changed and will recreate only the affected containers, while leaving the rest running without interruption.

### MediaWiki Connector Settings

For the Retriva MediaWiki Connector, configuration parameters defined in `config.yaml` can be overridden using environment variables prefixed with `MEDIAWIKI_CONNECTOR_`. 

For example, to override `sync_interval_minutes` and `target_kb_id` globally, you would add:
```env
MEDIAWIKI_CONNECTOR_SYNC_INTERVAL_MINUTES=30
MEDIAWIKI_CONNECTOR_TARGET_KB_ID=my_custom_kb
```
Note: Secrets (like `MEDIAWIKI_BOT_PASSWORD`) don't require this prefix and use their standard names.

### Multiple MediaWiki Instances

Retriva Pro supports syncing multiple MediaWiki websites simultaneously. Each wiki gets its own connector container with its own configuration, state, and tag. Chunks from different wikis are distinguished by the `tag` field in their metadata, allowing you to filter search results by source wiki.

#### Setup

To add a second MediaWiki instance:

**1. Create a config file**

Copy the default config and adjust the wiki-specific settings:

```bash
cp config/mediawiki.yaml config/mediawiki-2.yaml
```

Edit `config/mediawiki-2.yaml` — at minimum change `wiki_id` and `source_id`:

```yaml
wiki_id: secondwiki
source_id: src_mediawiki_2
api_url: https://second.wiki.example/api.php
# ... other settings
```

**2. Add environment variables to `.env`**

Add a `MEDIAWIKI_CONNECTOR_2_*` block for the second wiki:

```env
# --- MediaWiki Connector #2 ---
MEDIAWIKI_CONNECTOR_2_API_URL=https://second.wiki.example/api.php
MEDIAWIKI_CONNECTOR_2_PUBLIC_URL=https://second.wiki.example/api.php
MEDIAWIKI_CONNECTOR_2_TAG=wiki-2
MEDIAWIKI_CONNECTOR_2_AUTH_MODE=none
MEDIAWIKI_CONNECTOR_2_USERNAME=
MEDIAWIKI_CONNECTOR_2_BOT_PASSWORD=
MEDIAWIKI_CONNECTOR_2_SOURCE_ID=src_mediawiki_2
MEDIAWIKI_CONNECTOR_2_TARGET_KB_ID=default
MEDIAWIKI_CONNECTOR_2_COMMAND=daemon
```

**3. Uncomment the second connector in `docker-compose.yml`**

Find the commented `retriva-mediawiki-connector-2` service block and uncomment it. The block is pre-configured to read from `MEDIAWIKI_CONNECTOR_2_*` env vars and mount `config/mediawiki-2.yaml`.

**4. Rebuild and start**

```bash
./scripts/manage.sh build
./scripts/manage.sh up-pro
```

#### How Tags Work

Each connector tags every ingested chunk with a `tag` value in its `user_metadata`:

| Connector | Env var                     | Tag value | Example                        |
| --------- | --------------------------- | --------- | ------------------------------ |
| Wiki 1    | `MEDIAWIKI_CONNECTOR_TAG`   | `wiki-1`  | `user_metadata.tag = "wiki-1"` |
| Wiki 2    | `MEDIAWIKI_CONNECTOR_2_TAG` | `wiki-2`  | `user_metadata.tag = "wiki-2"` |

If `tag` is not set, it falls back to `wiki_id`. You can use this tag to filter search results or chat queries to a specific wiki using metadata filters:

```json
{
  "metadata_filters": [
    {"field": "user_metadata.tag", "operator": "eq", "value": "wiki-2"}
  ]
}
```

#### Managing Individual Connectors

Each connector runs in its own container with a unique name. You can manage them independently:

```bash
# View logs for a specific connector
./scripts/manage.sh logs retriva-mediawiki-connector
./scripts/manage.sh logs retriva-mediawiki-connector-2

# Restart a specific connector
./scripts/manage.sh restart retriva-mediawiki-connector-2

# Exclude a specific connector from startup
./scripts/manage.sh --exclude retriva-mediawiki-connector-2 up-pro
```

#### Adding More Wikis

Follow the same pattern for additional wikis:
1. Create `config/mediawiki-3.yaml`
2. Add `MEDIAWIKI_CONNECTOR_3_*` env vars
3. Copy and customize a `retriva-mediawiki-connector-3` service block in `docker-compose.yml`
4. Add a `mediawiki_connector_3_state` volume
## Managing Multiple Deployments (Tagging)

If you need to manage multiple deployments side-by-side (e.g., one for Customer A and one for Customer B), you can use the `COMPOSE_PROJECT_NAME` variable.

Docker Compose uses this name to namespace all containers, volumes, and networks. 

To create a separated deployment:
1. Copy `.env` to a new file, e.g., `.env.customer-a`
2. Inside `.env.customer-a`, change `COMPOSE_PROJECT_NAME` to a unique tag:
   ```env
   COMPOSE_PROJECT_NAME=retriva-customer-a
   ```
3. Run `manage.sh` using the new environment file:
   ```bash
   ENV_FILE=.env.customer-a ./scripts/manage.sh up
   ```

Because the project name is different, Docker will start a completely separate set of containers and create independent volumes (e.g., `retriva-customer-a_qdrant_storage`) that won't conflict with your other deployments!

## Notes

- This is a local development deployment.
- Ports are bound to localhost where convenient, but Qdrant/Ingestion/Core/Gateway/WebUI/Whisper are exposed on host ports for debugging.
- Do not use this unchanged in production.
- The Compose file intentionally uses `build:` for Retriva services, so it expects local repositories with Dockerfiles.
- The `whisper-init` container automatically downloads the `ggml-base.en.bin` model (or the model specified by `WHISPER_MODEL` in `.env`) from HuggingFace to the local volume before starting the whisper-server.

## Licensing

This deployment bundle is licensed under the Apache License 2.0. See the LICENSE file for details.

**Important — Retriva Core vs Retriva Pro:**

- **Retriva Core** components (`retriva-core`, `retriva-gateway`, `retriva-webui`) are licensed under the Apache License 2.0. The default `up` command starts only these components plus third-party infrastructure (Qdrant, Tika, Redis, Whisper), so a default deployment contains only Apache-2.0-licensed code.
- **Retriva Pro** is the commercial bundle formed by Retriva Core plus proprietary extension containers. Each extension is licensed separately and is **not** covered by the Apache 2.0 license of the core. Pro extensions are isolated behind the `pro` Docker Compose profile and are only started when you explicitly run `up-pro` (or the `up-with-connectors` alias).
- The first Pro extension is `retriva-mediawiki-connector`, licensed under the Retriva Pro Proprietary Commercial License Agreement (see `LICENSE.retriva-pro` in that repository).

This separation ensures that the Apache-2.0-licensed core remains self-contained and that proprietary code is never pulled into a deployment unless explicitly requested.