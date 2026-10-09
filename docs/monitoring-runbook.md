# Monitoring stack runbook (Spec 035 / ADR-040)

Canonical owner: `retriva-local-containerized-deployment` (Compose profile
`monitoring`). Platform: `prom/prometheus:v3.15.0`, `prom/alertmanager:v0.34.1`,
the read-only Redis exporter, the read-only PostgreSQL collector, and the local
alert sink. Private network only; no host ports are published.

Canonical names: every hostname used by the monitoring configuration is a
Compose service name of this repository's `docker-compose.yml`
(`retriva-prometheus`, `retriva-alertmanager`, `retriva-alert-sink`,
`retriva-redis-monitor-exporter`, `retriva-pg-monitor-exporter`). Short
harness-only names (for example `alertmanager`, `alert-sink`) are never valid
in this deployment; the committed tests reject them.

Operator invariants:

- The monitoring stack is read-only and unprivileged. It never administers
  Redis, PostgreSQL, or the application.
- Credentials are referenced, never printed. Do not paste rendered configs,
  environment files, ACL files, or scrape URLs into tickets or chats.
- Never test alerts by running destructive Redis commands **in production** —
  see [Destructive-test prohibition](#destructive-test-prohibition).

## Alert response index

| Alert | Section |
|---|---|
| RedisDeniedDestructiveCommand | [Destructive-command denial alert](#destructive-command-denial-alert) |
| RedisAuthFailuresAboveBaseline | [Authentication-failure alert](#authentication-failure-alert) |
| RedisTotalKeyCollapse, RedisQueueDisappearance | [Key queue unacked disappearance alert](#key-queue-unacked-disappearance-alert) |
| RedisBindingRecreationSpike | [Binding recreation spike](#binding-recreation-spike) |
| RedisResultRecordCollapse | [Result-record collapse](#result-record-collapse) |
| RedisRestartOrPersistenceAnomaly | [Persistence anomaly](#persistence-anomaly) |
| RedisEmergencyIdentityUse, RedisDefaultOrNopassAuthSuccess | [Emergency default identity alert](#emergency-default-identity-alert) |
| Monitoring* (self-monitoring) | [Monitoring stack unavailable](#monitoring-stack-unavailable), [Redis exporter authentication failure](#redis-exporter-authentication-failure), [PostgreSQL gauge collector failure](#postgres-gauge-collector-failure) |

For every alert: preserve evidence per
[Safe evidence collection](#safe-evidence-collection) before any corrective
action.

## Destructive-command denial alert

`RedisDeniedDestructiveCommand` (critical) fires when a normal identity
attempts a denied destructive/admin Redis command.

1. Treat as a potential security incident; do not dismiss as noise.
2. Preserve evidence (see below): `ACL LOG` entries (username, command,
   reason, context, count), service logs, `CLIENT LIST` summary.
3. Identify the caller: configuration drift, a legacy client, or external
   access. Confirm the port remains loopback/private only.
4. Escalate per [Escalation](#escalation). Do not disable denials to silence
   the alert; fix the caller or its configuration.

## Authentication-failure alert

`RedisAuthFailuresAboveBaseline` (warning) fires above five failures per 15
minutes.

1. Identify the client class (service vs unknown source) without logging
   credentials; `ACL LOG` `reason=auth` entries and `CLIENT LIST` bounds are
   sufficient.
2. A single planned deployment probe is expected; sustained failures indicate a
   stale credential, a misconfigured client, or an unknown consumer.
3. Rotate the affected role credential through the accepted interface if a
   client is stuck on an old value; verify recovery.

## Key queue unacked disappearance alert

`RedisTotalKeyCollapse` and `RedisQueueDisappearance` (critical) fire when key
counts collapse or the ingestion queue vanishes while durable non-terminal work
exists.

1. Correlate with PostgreSQL: see
   [Durable PostgreSQL reconciliation](#durable-postgresql-reconciliation).
2. Check Redis restart history and persistence status; confirm whether the
   instance was reset or restarted unexpectedly.
3. Treat flush-like symptoms as a security event; preserve `ACL LOG` and
   escalate.
4. Never restore stale Redis broker keys from an old RDB/AOF — see
   [Stale Redis restore prohibition](#stale-redis-restore-prohibition).

## Binding recreation spike

`RedisBindingRecreationSpike` (warning) fires when Kombu binding metadata churns
unusually (resets, restarts, or topology instability).

1. Check Redis restarts, exporter restarts, and worker/API reconnect storms.
2. Verify worker and ingestion health and that queue depth is consistent.
3. If churn is caused by a deployment or rollout, note it and confirm it
   settles; sustained churn needs a topology investigation.

## Result-record collapse

`RedisResultRecordCollapse` (critical) fires when result records fall below 20%
of the recent baseline beyond normal TTL behaviour.

1. Confirm normal expiry patterns (TTL-bound results shrink on their own).
2. Correlate with task activity and durable job aggregates; a collapse during
   steady completion is a reset/deletion symptom.
3. Preserve evidence and escalate; do not recreate results manually.

## Persistence anomaly

`RedisRestartOrPersistenceAnomaly` (warning) fires when the last RDB background
save did not succeed.

1. Check disk capacity and Redis logs.
2. Confirm ACL persistence is unaffected (`/run/redis-acl/users.acl`).
3. Fix persistence before any planned Redis restart; a restart without a
   healthy save can lose recent transient state.

## Emergency default identity alert

`RedisEmergencyIdentityUse` and `RedisDefaultOrNopassAuthSuccess` (critical).

1. Emergency use requires recorded owner approval and an audit of the session;
   if no approval exists, treat as a security incident.
2. Default/nopass success means the deny-by-default posture has regressed:
   inspect the ACL file and Redis configuration immediately; re-apply the
   committed configuration through the canonical deployment procedure.
3. Never enable default/emergency access to "fix" monitoring; the emergency
   identity stays disabled except under the governed break-glass procedure.

## Monitoring stack unavailable

Covers `MonitoringCollectorScrapeDown`, `MonitoringAlertmanagerDown`,
`MonitoringRuleEvaluationFailures`, `MonitoringConfigReloadFailed`, and
`MonitoringStorageHighUsage`.

1. Check container health with the canonical service names:
   `docker compose --profile monitoring ps retriva-prometheus
   retriva-alertmanager retriva-redis-monitor-exporter
   retriva-pg-monitor-exporter retriva-alert-sink`.
2. Validate configuration before restarting: `promtool check config` for
   Prometheus and `amtool check-config` for Alertmanager (or the equivalent
   pinned commands).
3. Reload through the supported mechanism (`POST /-/reload` when lifecycle is
   enabled) rather than recreating services when possible.
4. Capacity: review TSDB retention (`--storage.tsdb.retention.time`) and disk.
5. While monitoring is down, Redis security relies on the ACL LOG review and
   `scripts/redis-ops.sh verify-acl` from the application runbook; treat the
   gap as a priority incident.

## Redis exporter authentication failure

`MonitoringRedisExporterAuthFailures` (critical): the exporter's `rtrv-monitor`
credential failed.

1. Confirm the exporter uses the monitor identity only (no broker/results
   reuse).
2. Rotate/refresh the monitor credential through the accepted secret interface
   and restart only the exporter service.
3. Verify `redis_monitor_exporter_up` returns to 1 and Redis alerts resume.

## Postgres gauge collector failure

`MonitoringPostgresGaugeStale` (warning): `retriva_pg_nonterminal_jobs` has not
refreshed.

1. Check collector logs and the read-only role grant. Under the currently
   committed design the collector role has `CONNECT`, `USAGE` on schema
   `jobs`, and `SELECT` on `jobs.jobs` — no other grants.
2. Confirm the database is reachable and the query respects the statement
   timeout (`PGOPTIONS=-c statement_timeout=5000`).
3. Restart only the `retriva-pg-monitor-exporter` service after verifying the
   role.

**Known limitation (recorded 2026-10-09):** `jobs.jobs` carries
`FORCE ROW LEVEL SECURITY`; without a tenant or the controlled privileged
flag, the direct `SELECT count(*)` source observes zero rows — the gauge is
structurally `0` even while durable non-terminal jobs exist. The correct
source is the proposed aggregate-only interface in Spec 036 / ADR-041
(`jobs.monitoring_nonterminal_job_count()`, `EXECUTE`-only grants). Until
those are accepted and implemented, a monitoring deployment must not be
declared `CLOSED_SUCCESS` while this metric is relied upon, and operators
must treat the PostgreSQL term of `RedisQueueDisappearance` as unavailable.
After acceptance and implementation, verification is:

```sql
-- as the monitoring login role, through psql with the session timeout
SELECT jobs.monitoring_nonterminal_job_count();       -- correct cross-tenant count
SELECT count(*) FROM jobs.jobs;                       -- must be denied (no table SELECT)
```

plus catalog checks that `FORCE ROW LEVEL SECURITY` is still enabled and that
`PUBLIC` has no `EXECUTE` on the function.

## Safe evidence collection

- `ACL LOG <n>` via the `rtrv-monitor` identity (never print passwords).
- Service logs (`docker logs`), container health/restart counts, queue depth,
  key counts, binding counts, result-record counts through the collector
  metrics (Prometheus API) — aggregate values only.
- PostgreSQL aggregates via the read-only role (counts only).
- Never paste secrets, hashes, userinfo URLs, task/job identifiers, or payloads
  into tickets, chat, or reports.

## Destructive-test prohibition

Never run `FLUSHALL`, `FLUSHDB`, `SWAPDB`, `KEYS`, `CONFIG SET`, `ACL SETUSER`,
`SHUTDOWN`, `DEBUG`, `MONITOR`, arbitrary `EVAL`, or any destructive/admin
command against production Redis — including as an alert test. Denial
verification uses `ACL DRYRUN` via `scripts/redis-ops.sh verify-acl`. Alert
firing is verified with synthetic series (`promtool test rules`), with the local
sink, and with non-destructive signals (a benign denied read like `EXISTS` under
an under-privileged role, TTL expiry, and exporter-side transitions).

## Durable PostgreSQL reconciliation

When Redis and PostgreSQL disagree (queue/key collapse, redelivery doubts):

1. PostgreSQL is the authority for durable jobs; compare non-terminal aggregates
   (`retriva_pg_nonterminal_jobs`) with queue/unacked metrics.
2. Use the read-only reconciliation dry-runs (`python -m retriva.jobs.reconcile
   --dry-run`) for analysis; never apply reconciliation as part of alert
   response without a governed task.
3. Do not replay tasks or recreate results manually.

## Stale Redis restore prohibition

Never restore an old Redis RDB/AOF or copy stale broker keys into the live
instance to "repair" a disappearance alert. Stale restoration can resurrect
already-processed work. Recovery, if needed, follows the governed reconciliation
path with PostgreSQL as the authority.

## Escalation

- Security/incident alerts (A1, A3 flush-like, A6): owner/security contact
  immediately, with preserved evidence.
- Availability/correlation alerts (A2, A4, A5, persistence, self-monitoring):
  platform operator first; escalate to the owner on persistence or recurrence.
- Contact roles, not personal secret data: platform operator, security owner,
  durable-jobs owner. Record the escalation in the incident log.

## Silencing and maintenance

- Silence through Alertmanager (`amtool silence add ...` or the Alertmanager
  API) with a bounded duration, an explicit reason, and the acting operator.
- Silences must never hide A6 (emergency/default) or A1 (destructive denial)
  without owner approval.
- Planned maintenance that stops Redis or the collectors should be silenced
  narrowly (collector down) and unsilenced immediately afterwards.

## Canonical deployment and verification commands

Deployment is dependency-safe and strictly monitoring-only. Never run the
profile without `--no-deps` while the application services are live, and
never recreate application dependencies as part of a monitoring change.

```bash
# deploy or update the monitoring profile only
docker compose --profile monitoring up -d --no-deps retriva-prometheus retriva-alertmanager retriva-alert-sink retriva-redis-monitor-exporter retriva-pg-monitor-exporter

# stop and remove only the monitoring services (volumes preserved for evidence)
docker compose --profile monitoring stop retriva-prometheus retriva-alertmanager retriva-alert-sink retriva-redis-monitor-exporter retriva-pg-monitor-exporter
docker compose --profile monitoring rm -sf retriva-prometheus retriva-alertmanager retriva-alert-sink retriva-redis-monitor-exporter retriva-pg-monitor-exporter

# configuration reload (lifecycle enabled)
docker exec retriva-prometheus wget -qO- --post-data= http://127.0.0.1:9090/-/reload
```

Never run a bare profile-level `down`: base-profile services are always
enabled and would be removed with it. Always use the explicit five-service
stop/remove form above.

Target and routing verification (canonical DNS names only):

```bash
# every scrape target must be up by canonical name
docker exec retriva-prometheus wget -qO- http://127.0.0.1:9090/api/v1/targets
# 16 rules in two groups, health ok
docker exec retriva-prometheus wget -qO- http://127.0.0.1:9090/api/v1/rules
# safe routing test: synthetic alert through the Alertmanager API, then
# confirm delivery on the local sink and let the test alert expire
# (POST http://retriva-alertmanager:9093/api/v2/alerts; GET
#  http://retriva-alert-sink:9099/alerts). No external notification occurs.
```

Derived collector image policy: `retriva-pg-monitor-exporter:local` is built
at deploy time from the committed `Dockerfile.pg-monitor` (the base tag
`postgres:16.15-alpine` and the `apk` package revision can drift), so the
built digest must be recorded at validation time and the monitoring
containers must run exactly that recorded digest. Do not deploy a rebuild
that was not validated. The same containment applies to the pinned
Prometheus, Alertmanager, and python:3.12-alpine image digests.

## Rollback / removal

- The monitoring profile is additive. Stop and remove only the monitoring
  services with the explicit five-service commands in
  [Canonical deployment and verification commands](#canonical-deployment-and-verification-commands);
  application and Redis state are untouched.
- To remove permanently: remove the monitoring services/volumes from the
  deployment compose and the `config/monitoring/` tree; revoke the read-only
  PostgreSQL monitor role (currently `SELECT`-based; after Spec 036 / ADR-041
  acceptance, revoke `EXECUTE` on the interface instead).
- Rolling back a bad rule/config change: restore the previous committed
  revision, validate with `promtool`, and reload.
