# PostgreSQL Business Intelligence Database (Retriva Pro)

PostgreSQL is the authoritative **relational** store for Retriva's
business data: canonical organization identity, names and aliases,
external identifiers, domains, corporate relationships, commercial
roles, qualification lifecycle, campaign exclusions, source
observations, ERP import staging, and the append-only audit trail.

It does **not** replace Qdrant:

| Store | Purpose |
|---|---|
| PostgreSQL (`retriva-postgres`) | canonical identities, business facts, roles, lifecycle, qualification history, exclusions, workflow, imports, audit |
| Qdrant | embeddings and retrieval-oriented representations |
| Durable artifact storage | uploaded files, generated reports |
| Existing provider-resource storage | unchanged until its own migration phase |

The stack is gated behind the `db` Compose profile: `up`, `up-pro` and
other profiles are completely unaffected, and `db-down` +
`CRM_PG_ENABLED=false` reverses the phase (volumes are kept by default).

## Services

| Service | Role |
|---|---|
| `retriva-postgres` | PostgreSQL 16 (pinned image `postgres:16.15-alpine`), persistent volume `retriva_pg_data`, **internal Docker network only** — never published to the host |
| `retriva-pg-bootstrap` | one-shot: creates/rotates the five managed roles using the cluster admin connection (deployment time only) |
| `retriva-pg-migrate` | one-shot: applies versioned SQL migrations with a ledger (migrator role only) |
| `retriva-pgadmin` | pgAdmin (pinned image `dpage/pgadmin4:9.18`), persistent volume `retriva_pgadmin_data`, binds to `127.0.0.1:5050` by default |

The `latest` tag is never used; both images are pinned to specific
supported versions and can be overridden (`RETRIVA_PG_IMAGE`,
`RETRIVA_PGADMIN_IMAGE`).

## Quick start

1. Copy the PostgreSQL block from `.env.example` into your `.env` and
   fill every credential. Generate local secrets with:

   ```bash
   python -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

   Every `*_PASSWORD` also accepts a `*_PASSWORD_FILE` path (Docker
   secret or mounted file) instead of a value. Never commit real
   credentials.

2. Start the stack:

   ```bash
   ./scripts/manage.sh db-up
   ```

   This starts PostgreSQL, waits for health, runs the role bootstrap,
   applies all migrations, and launches pgAdmin. The command fails
   fast with a precise list of missing variables.

3. Activate the runtime store (optional, reversible):

   ```bash
   # .env
   CRM_PG_ENABLED=true
   ```

   then restart the Pro services. The readiness endpoint becomes
   available at `http://localhost:8001/api/v2/crm/pg/readiness`
   (core service port) and reports connectivity, schema revision,
   pending migrations and role access — never credentials.

## Operations

```bash
./scripts/manage.sh db-up         # start postgres + bootstrap + migrate + pgadmin
./scripts/manage.sh db-down       # stop the stack (volumes kept)
./scripts/manage.sh db-migrate    # apply pending migrations
./scripts/manage.sh db-status    # ledger + pending migration list
./scripts/manage.sh db-verify    # RLS/role invariant checks
./scripts/manage.sh db-readiness # readiness report (no credentials)
./scripts/manage.sh db-psql      # psql shell inside the container
./scripts/manage.sh db-logs -f   # follow postgres logs
```

### Migrations

Migrations are versioned SQL files shipped inside the
`retriva_crm_assistant` package (`postgres/sql/V<NNN>__<name>.up.sql`
plus a matching `.down.sql`). The runner:

- applies them through the dedicated one-shot step — application
  processes never race to apply migrations (a session advisory lock
  also serializes concurrent runners);
- records every applied version, checksum, timestamp and applying
  role in the `audit.schema_migrations` ledger;
- wraps each migration in a single transaction (DDL, ledger insert, or
  nothing);
- supports downgrades only with an explicit `--confirm-destructive`
  acknowledgement — nothing is ever silently destroyed;
- exposes `status` (ledger vs shipped files) and `verify` (RLS on
  every tenant table, forced RLS, role privilege posture, audit
  append-only, schema ownership).

### Database roles

| Role | Privileges |
|---|---|
| `retriva_migrator` | owns all schemas; the only role that applies schema changes |
| `retriva_application` | SELECT/INSERT/UPDATE on business/qualification/research/jobs/imports; no DELETE on business history; **no DDL, owns nothing** |
| `retriva_importer` | writes only `imports.*` (staging + commit paths); read-only elsewhere |
| `retriva_readonly` | SELECT only |
| `retriva_pgadmin_operator` | SELECT only (explicit pgAdmin operator posture) |

No production service uses the PostgreSQL superuser; the cluster admin
(`retriva_admin`) is used only by the one-shot bootstrap step.

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
state.

### Audit

`audit.events` is append-only: writers hold SELECT/INSERT only, and a
trigger rejects UPDATE/DELETE regardless of role — even the cluster
administrator cannot rewrite history. Secrets and complete provider
payloads are scrubbed from audit metadata before storage.

## Connecting pgAdmin to PostgreSQL

pgAdmin is pre-registered with the internal server definition in
`config/pgadmin/servers.json` (no credentials stored in it):

1. Open `http://127.0.0.1:5050`.
2. Log in with `RETRIVA_PGADMIN_EMAIL` / `RETRIVA_PGADMIN_UI_PASSWORD`
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

### Network policy

- PostgreSQL has **no published ports**: it is reachable only inside
  the `retriva-net` Docker network.
- pgAdmin binds to `127.0.0.1` by default (`RETRIVA_PGADMIN_BIND_ADDR`).
- Remote access requires an SSH tunnel, VPN, or an authenticated
  internal route — for example:

  ```bash
  ssh -L 5050:127.0.0.1:5050 user@retriva-host
  ```

## Backup and restore

```bash
# Backup
docker exec retriva-postgres pg_dump -U retriva_admin -d retriva \
  -Fc > retriva-pg-$(date +%F).dump

# Restore into a fresh volume
docker exec -i retriva-postgres pg_restore -U retriva_admin -d retriva \
  --clean --if-exists < retriva-pg-2026-09-22.dump
```

## Troubleshooting

- `db-up` fails with a list of missing variables — fill them in
  `.env` (values or `*_FILE` paths) and re-run.
- Bootstrap/migration one-shots exit non-zero: check
  `./scripts/manage.sh db-logs` and `docker logs retriva-pg-bootstrap`;
  messages never contain credentials.
- Readiness reports `pending_migrations > 0`: run
  `./scripts/manage.sh db-migrate`.
- Readiness reports `status: "disabled"`: `CRM_PG_ENABLED` is false —
  the runtime store is not activated (fully reversible).
