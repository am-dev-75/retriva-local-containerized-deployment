# Shared PostgreSQL Platform (Retriva Core and Pro)

PostgreSQL is **mandatory for every Retriva deployment** — including
Core-only development deployments — and is the authoritative
**relational** store for Retriva's business data: canonical
organization identity, names and aliases, external identifiers,
domains, corporate relationships, commercial roles, qualification
lifecycle, campaign exclusions, source observations, ERP import
staging, and the append-only audit trail.

The architecture (Spec 024; ADR-029) is:

- **one shared PostgreSQL instance** (`retriva-postgres`);
- **one database** named `retriva`;
- **multiple module-owned schemas** — isolation is by schema
  ownership and PostgreSQL privileges, never by separate
  databases;
- **independently versioned migration streams** registered through
  a Core-owned provider contract:
  `core.platform` (Core), `pro.crm` (CRM Assistant, Pro),
  `pro.messaging` (Messaging, Pro);
- **dedicated roles**: bootstrap (admin, deployment-time only),
  migrator, Core runtime, and per-extension runtime identities.

It does **not** replace Qdrant:

| Store | Purpose |
|---|---|
| PostgreSQL (`retriva-postgres`, `retriva` database) | migration ledger (`platform` schema), CRM business facts (business/qualification/research/imports/jobs/audit/campaigns), future Core tables, future `messaging` schema |
| Qdrant | embeddings and retrieval-oriented representations |
| Durable artifact storage | uploaded files, generated reports |
| Existing provider-resource storage | unchanged until its own migration phase |

**Not migrated in this phase:** Qdrant, Redis/Celery state, GraphRAG
SQLite state, connector caches (MediaWiki, Email Agent), documents,
attachments, and model artifacts all stay exactly where they are.
This phase targets development and testing, not production
readiness.

## Schema ownership

| Schema | Owner | Stream |
|---|---|---|
| `platform` | Core (`core.platform`): the shared migration ledger | Core, always installed |
| `business`, `qualification`, `research`, `imports`, `jobs`, `audit`, `campaigns` | CRM Assistant (Pro) | `pro.crm`, installed when the Pro extension is deployed |
| `messaging` | Retriva Messaging (Pro) | `pro.messaging`, shared-database target of the Messaging draft |

Core creates only Core-owned objects. A Pro installation first
establishes Core database state (bootstrap + `core.platform`), then
adds its extension schemas. Core never depends on Pro schemas,
packages, or migration providers.

The `audit` schema is mixed by history: `audit.events` is the CRM
audit domain (unchanged, append-only); the retired
`audit.schema_migrations` ledger table is preserved read-only after
adoption of its rows into `platform.schema_migrations`. The `jobs`
schema is a CRM-created placeholder with no tables; its transfer to
a Core-owned durable-jobs stream is a documented follow-up
(Spec 024 plan.md §9), not done here.

## Services

| Service | Profile | Role |
|---|---|---|
| `retriva-postgres` | — (always) | PostgreSQL 16 (pinned `postgres:16.15-alpine`), persistent volume `retriva_pg_data`, **internal Docker network only** |
| `retriva-pg-bootstrap` | — (always) | one-shot: Core platform roles + database CREATE restriction (admin connection, deployment time only) |
| `retriva-pg-migrate` | — (always) | one-shot: applies the `core.platform` stream (migrator credential only) |
| `retriva-pg-crm-bootstrap` | `pro`, `db` | one-shot: the four CRM extension roles |
| `retriva-pg-crm-migrate` | `pro`, `db` | one-shot: applies the `pro.crm` stream after the Core migrations |
| `retriva-pg-messaging-bootstrap` | `pro`, `messaging` | one-shot: `retriva_messaging` runtime role + `messaging` schema grants |
| `retriva-pg-messaging-migrate` | `pro`, `messaging` | one-shot: applies Messaging's Alembic stream to the `messaging` schema (migrator identity) |
| `retriva-pgadmin` | `db`, `pgadmin` | pgAdmin operator view (pinned `dpage/pgadmin4:9.18`), binds `127.0.0.1:5050` |

The `latest` tag is never used; both images are pinned to specific
supported versions and can be overridden (`RETRIVA_PG_IMAGE`,
`RETRIVA_PGADMIN_IMAGE`).

## Startup order

- **Core-only** (`./scripts/manage.sh up`): PostgreSQL healthy →
  platform bootstrap succeeds → Core migrations (`core.platform`)
  succeed → Core services start. No Pro package, no Pro credential,
  no Pro schema is required. The Core services and the platform
  one-shots build the Core-only `base` stage of the core Dockerfile
  (the `RETRIVA_CORE_BUILD_TARGET` / `RETRIVA_PG_BUILD_TARGET`
  defaults; the base image contains no CRM Assistant or Messaging
  package).
- **Pro** (`./scripts/manage.sh up-pro`): the Core sequence above,
  then the CRM bootstrap and the `pro.crm` migrations (adopting the
  retired ledger history where present), then the Pro services that
  need them. Core services never depend on optional Pro migrations:
  an extension migration failure is isolated to that extension's
  one-shot and surfaces there, never hidden by a Core service.
- **Messaging** (when `RETRIVA_MESSAGING_ENABLED=on`): after the
  Core migrations, the Messaging bootstrap + Alembic one-shots, then
  the Messaging API.

All one-shots are idempotent: a repeated `docker compose up` (or a
re-run of `db-migrate`) applies nothing when everything is
up-to-date and never damages valid database objects. The
`retriva_pg_data` volume is always preserved.

## Configuration

Canonical names (`.env`, per deployment):

- `RETRIVA_PG_HOST` / `RETRIVA_PG_PORT` / `RETRIVA_PG_DATABASE` /
  `RETRIVA_PG_SSLMODE` — shared endpoint (`retriva` database);
- `RETRIVA_PG_ADMIN_USER` + `RETRIVA_PG_ADMIN_PASSWORD(_FILE)` —
  cluster administrator (bootstrap only);
- `RETRIVA_PG_MIGRATOR_PASSWORD(_FILE)` — the platform migrator
  (owns schema changes; the development-phase shared-migrator
  decision is recorded in ADR-029);
- `RETRIVA_PG_CORE_PASSWORD(_FILE)` — the Core runtime identity;
- extension runtime credentials stay extension-specific:
  `CRM_PG_APPLICATION_*`, `CRM_PG_IMPORTER_*`, `CRM_PG_READONLY_*`,
  `CRM_PGADMIN_UI_OPERATOR_*`, `RETRIVA_MESSAGING_DB_PASSWORD`.

Deprecated compatibility names (still accepted, mapped to the
canonical `RETRIVA_PG_*` values): `CRM_PG_HOST`, `CRM_PG_PORT`,
`CRM_PG_DATABASE`, `CRM_PG_SSLMODE`, `CRM_PG_ADMIN_USER`,
`CRM_PG_ADMIN_PASSWORD(_FILE)`, `CRM_PG_MIGRATOR_PASSWORD(_FILE)`,
`CRM_PG_POOL_*`, `CRM_PG_*_TIMEOUT*`. Set the `RETRIVA_PG_*`
canonical names instead; the CRM-prefixed spellings will be removed
by a future governed change.

Removed variables: `RETRIVA_PG_TOOLS_BUILD_TARGET` (the platform
one-shots now always build the Core-only `base` stage via
`RETRIVA_PG_BUILD_TARGET`; the CRM one-shots always build the `pro`
stage via `RETRIVA_PG_CRM_BUILD_TARGET`), `MESSAGING_DB_NAME`,
`MESSAGING_DB_USER`, `MESSAGING_DB_PASSWORD` (replaced by
`RETRIVA_MESSAGING_DB_USER` / `RETRIVA_MESSAGING_DB_PASSWORD`
against the shared database).

Never commit real credentials: every `*_PASSWORD` accepts a
`*_PASSWORD_FILE` path (Docker secret or mounted file); generate
local secrets with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'`.
Passwords never appear in logs, readiness output, health checks, or
error messages.

## Operations

```bash
./scripts/manage.sh up            # Core-only: includes the mandatory PG lifecycle
./scripts/manage.sh up-pro        # Pro: adds CRM bootstrap + pro.crm migrations
./scripts/manage.sh db-up         # additionally start the pgAdmin operator view
./scripts/manage.sh db-down      # stop ONLY pgAdmin (PostgreSQL stays)
./scripts/manage.sh db-migrate    # apply pending migrations (core.platform, then pro.crm when enabled)
./scripts/manage.sh db-status     # ledger + pending migration list
./scripts/manage.sh db-verify    # framework + CRM RLS/role invariant checks
./scripts/manage.sh db-readiness # readiness report (no credentials)
./scripts/manage.sh db-psql      # psql shell inside the container
./scripts/manage.sh db-logs -f   # follow postgres logs
```

### Migration providers

Migrations are plain versioned SQL streams registered through the
Core provider contract (`retriva.infrastructure.postgres`):

- provider identity + stream identity + version + name + sha256
  checksum + up/down SQL bodies;
- streams are ordered deterministically (dependencies first;
  `pro.crm` depends on `core.platform`);
- `core.*` streams are namespace-reserved to the `retriva-core`
  provider — an extension cannot overwrite or impersonate them;
- the applied history is recorded in `platform.schema_migrations`
  under (provider, stream, version) with name, checksum, timestamp
  and applying identity;
- every migration runs in a single transaction (DDL + ledger insert,
  or nothing), a session advisory lock serializes concurrent
  runners, and applied checksums are verified — an edited applied
  file fails safely;
  downgrades require `--confirm-destructive` (and are refused while
  persisted ACP history exists);
- the retired CRM ledger (`audit.schema_migrations`) is adopted
  into the Core ledger with identity preserved (version, name,
  checksum, applied timestamp/identity) and left untouched
  thereafter.

**`RETRIVA_EXTENSIONS` vs `RETRIVA_PG_MIGRATION_PROVIDERS`.** These
two lists serve different layers and are intentionally separate:

- `RETRIVA_EXTENSIONS` — which extension *packages* the running
  services load (runtime code, routers, stores);
- `RETRIVA_PG_MIGRATION_PROVIDERS` — which *migration streams* the
  Core migration CLI applies (passed to the one-shot; a module path
  exposing `MIGRATION_PROVIDERS`).

In this deployment the CRM one-shot does not use the env list at
all: `retriva-pg-crm-migrate` runs the CRM Assistant CLI
(`python -m retriva_crm_assistant.postgres.migrate upgrade`), which
registers its own provider (`pro.crm`) directly — the extension and
its migration stream therefore cannot drift apart in the Compose
topology. The env list exists for deployments that drive everything
through the Core CLI (`python -m retriva.infrastructure.postgres.
migrate upgrade`). Drift behavior, by scenario:

1. **Extension enabled, provider omitted** — the extension's code
   loads but its schema is never applied. The store fails closed:
   CRM requests return 503 (`CRM_PG_ENABLED` gate), `db-status`
   shows the stream pending, and `db-verify` fails clearly. Nothing
   starts against a silently missing schema.
2. **Provider configured, extension disabled** — migrations still
   apply (the schema exists), the runtime store stays off
   (`CRM_PG_ENABLED=false` → reversible phase; requests 503).
3. **Provider module missing/unimportable** — the migration
   one-shot fails immediately with a `MigrationError` naming the
   module and `RETRIVA_PG_MIGRATION_PROVIDERS`; the one-shot exits
   non-zero and any service depending on it does not start.
4. **Duplicate provider/stream configured** — registration fails
   clearly (`MigrationError`); duplicate provider/stream/version
   identities are never silently merged.
5. **Core-only deployment** — no provider module is listed; only
   `core.platform` applies; no Pro credential, schema, or package
   is involved.
6. **CRM deployment** — the `pro.crm` stream applies after
   `core.platform` through the CRM one-shot (see above).
7. **Messaging** — deliberately NOT a provider-contract stream yet:
   it retains ownership of its Alembic stream (`pro.messaging` is
   its documented target identity; wiring its execution through the
   Core contract is deferred to the Messaging validation phase).

### Database roles

| Role | Privileges |
|---|---|
| `retriva_admin` (or the container bootstrap user) | cluster administrator — bootstrap one-shots only, never a service runtime identity |
| `retriva_migrator` | owns all module schemas and the only role that applies schema changes (development-phase shared migrator; boundaries explicit) |
| `retriva_core` | Core runtime: USAGE on `platform` + SELECT on the migration ledger; no CRM grant (cannot read or write CRM tables) |
| `retriva_application` | CRM runtime DML (business/qualification/research/jobs/imports/campaigns); no DELETE on history; append-only tables stay INSERT-only; **no DDL, owns nothing** |
| `retriva_importer` | writes only `imports.*` (staging + commit paths); read-only elsewhere |
| `retriva_readonly` | SELECT only |
| `retriva_pgadmin_operator` | SELECT only (explicit pgAdmin operator posture) |
| `retriva_messaging` | Messaging runtime: USAGE + DML on the Pro-owned `messaging` schema only |

### Campaign tracking (V006)

Migration V006 adds the `campaigns` schema: campaign registry,
campaign relationships, company-level campaign participation with an
append-oriented event history (the addressed invariant: an
organization is addressed iff a non-revoked `ADDRESS_CONFIRMED` event
exists), versioned selection policies and explainable audience
selection runs. Row-Level Security covers every campaigns table with
the same fail-closed `app.current_tenant` mechanism; the pgAdmin
operator inspects campaigns through the tenant-scoped read-only
queries in the CRM extension's `docs/campaign-inspection-queries.md`.

### ERP customer identity and addresses (V007)

Migration V007 makes the database fully ready for the hierarchical
Sage X3 customer export (`F1790281894348.txt`-style semicolon TXT):
`SOURCE_CONFIRMED` identifier verification (ERP customer codes are
source-confirmed; ERP-provided VAT/names stay `UNVERIFIED`), the
controlled `business.source_systems` registry (stable `SAGE_X3`
namespace; single-dossier assumption documented in ADR-021),
`business.organization_addresses` (dated, hash-deduplicated address
history), `business.organization_contact_points` (company-level
only) and `business.organization_identifier_lineage` (import batch /
file / B-row lineage per observation). Identifier lookup is
merge-safe (chains through `MERGED` shells to the surviving
canonical organization, cycles fail closed). Contact (C) and banking
(R) records from the export are never persisted — at most their row
numbers and ignored reasons are staged; the uploaded file is not
retained on disk. Tenant configuration:
`CRM_ERP_SOURCE_SYSTEM_CODE`, `CRM_ERP_INTERNAL_CUSTOMER_CODES`,
`CRM_ERP_INTERNAL_VAT_VALUES`, `CRM_ERP_TEST_CUSTOMER_CODES`. The
pgAdmin inspection queries live in the CRM extension's
`docs/erp-inspection-queries.md`.

### Tenant isolation

Every tenant-owned table carries `tenant_id` and enforces PostgreSQL
Row-Level Security keyed on the transaction-local `app.current_tenant`
setting:

- missing tenant context fails closed (no rows visible or writable);
- one tenant cannot read or modify another tenant's organizations;
- import batches, assessment data and audit events are tenant-scoped;
- test tenants cannot touch `cust_0007`.

The tenant context is set per transaction by the connection pool
(`set_config(..., true)`), so pooled connections never leak tenant
state. The Core migration ledger (`platform.schema_migrations`) is
deployment-global infrastructure and is deliberately not
tenant-scoped.

### Audit

`audit.events` is append-only: writers hold SELECT/INSERT only, and a
trigger rejects UPDATE/DELETE regardless of role — even the cluster
administrator cannot rewrite history. Secrets and complete provider
payloads are scrubbed from audit metadata before storage.

## Connecting pgAdmin to PostgreSQL

pgAdmin is pre-registered with the internal server definition in
`config/pgadmin/servers.json` (no credentials stored in it):

1. Open `http://127.0.0.1:5050`.
2. Log in with `RETRIVA_PGADMIN_UI_EMAIL` / `RETRIVA_PGADMIN_UI_PASSWORD`
   (the email is optional and defaults to `ops@example.com`; reserved
   domains such as `.local` are rejected by pgAdmin's validation).
3. The server **Retriva PostgreSQL (internal)** is pre-registered with:
   - Host: `retriva-postgres` (the internal Docker hostname — use it,
     not `localhost`, because pgAdmin runs in its own container);
   - Port: `5432`;
   - Database: `retriva` (value of `RETRIVA_PG_DATABASE`);
   - Username: `retriva_pgadmin_operator`;
   - Password: your `CRM_PGADMIN_UI_OPERATOR_PASSWORD`.
4. Click Save — the connection is stored in pgAdmin's own volume.

> **Important — first initialization only:** `RETRIVA_PGADMIN_UI_EMAIL`
> and `RETRIVA_PGADMIN_UI_PASSWORD` seed pgAdmin's initial user the
> FIRST time the `retriva_pgadmin_data` volume boots; later changes to
> those variables are ignored by the running instance.  To apply new
> UI credentials, stop `retriva-pgadmin`, remove its container and the
> `retriva_pgadmin_data` volume (pgAdmin's own config only — the
> PostgreSQL data volume is untouched), then start it again.

### Network policy

- PostgreSQL has **no published ports**: it is reachable only inside
  the `retriva-net` Docker network.
- pgAdmin binds to `127.0.0.1` by default (`RETRIVA_PGADMIN_BIND_ADDR`).
- Remote access requires an SSH tunnel, VPN, or an authenticated
  internal route — for example:

  ```bash
  ssh -L 5050:127.0.0.1:5050 user@retriva-host
  ```

## Backup, restore, and reset

```bash
# Development backup BEFORE a migration (always do this first):
docker exec retriva-postgres pg_dump -U retriva_admin -d retriva \
  -Fc > retriva-pg-$(date +%F).dump

# Inspect migration status:
./scripts/manage.sh db-status

# Open a PostgreSQL shell:
./scripts/manage.sh db-psql

# Restore a backup into a THROWAWAY database for validation:
docker exec -i retriva-postgres psql -U retriva_admin \
  -c 'CREATE DATABASE retriva_restore_check' && \
docker exec -i retriva-postgres pg_restore -U retriva_admin \
  -d retriva_restore_check --clean --if-exists < retriva-pg-2026-10-04.dump
# ... validate there, then drop it:
docker exec retriva-postgres psql -U retriva_admin \
  -c 'DROP DATABASE retriva_restore_check'

# Reset ONLY disposable development state (never the PG volume):
docker compose --project-name "$PROJECT_NAME" down
docker volume rm "${PROJECT_NAME}_qdrant_storage"   # example
```

The `retriva_pg_data` volume and all CRM data are never recreated,
truncated, or dropped by the lifecycle; only a deliberate, explicit
operator action can touch them.

## Troubleshooting

- `up` / `up-pro` fails with a list of missing variables — fill them
  in `.env` (values or `*_FILE` paths) and re-run.
- Bootstrap/migration one-shots exit non-zero: check
  `./scripts/manage.sh db-logs` and `docker logs retriva-pg-bootstrap`;
  messages never contain credentials.
- Readiness reports `pending_migrations > 0`: run
  `./scripts/manage.sh db-migrate`.
- Readiness reports `status: "disabled"`: `CRM_PG_ENABLED` is false —
  the CRM runtime store is not activated (fully reversible; the shared
  platform itself still runs).
- A Core service waits at start: it depends on
  `retriva-pg-migrate` succeeding — check
  `docker logs retriva-pg-migrate`.
- The Messaging draft: `db-messaging-bootstrap` +
  `db-messaging-migrate` provision and migrate its `messaging`
  schema on the shared database; the dedicated
  `retriva-messaging-db` service no longer exists.
