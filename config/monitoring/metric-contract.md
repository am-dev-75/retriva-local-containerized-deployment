# Spec 035 / ADR-040 — monitoring metric contract (normative)

All metrics are produced by read-only collectors. No metric, label, annotation,
log line, or fixture may contain credentials, password hashes, ACL material,
userinfo URLs, command arguments, arbitrary key names, result payloads, or
tenant/job/task/document identifiers. Label values are bounded enumerations.

## Redis exporter (`redis-monitor-exporter`, identity `rtrv-monitor`)

| Metric | Type | Labels | Source | Healthy baseline | Failure semantics |
|---|---|---|---|---|---|
| `redis_monitor_exporter_up` | gauge | – | exporter poll loop | 1 | 0 on connection/auth/parse failure |
| `redis_monitor_exporter_last_success_timestamp_seconds` | gauge | – | exporter | now-15s | stale when polling fails |
| `redis_monitor_exporter_auth_failures_total` | counter | – | exporter-side AUTH/WRONGPASS/NOAUTH/NOPERM | 0 | grows on credential problems |
| `redis_monitor_exporter_scrape_errors_total` | counter | – | exporter | 0 | grows on connection errors |
| `redis_up` | gauge | – | `PING` | 1 | 0 when Redis is unreachable |
| `redis_db_keys` | gauge | `db` (db0…dbN, bounded to configured logical DBs) | `INFO keyspace` | 3 keys (db0), 1 (db1) in the steady hardened stack | 0/stale when Redis unavailable |
| `redis_key_exists` | gauge | `key` ∈ {ingestion} | `EXISTS` | 0 when idle with nothing queued | 0 with durable work ⇒ queue-disappearance alert |
| `redis_queue_depth` | gauge | `queue` ∈ {ingestion} | `LLEN` | 0 when idle | grows with backlog |
| `redis_unacked_present` | gauge | – | `EXISTS unacked` | 0 | 1 while deliveries are unacknowledged |
| `redis_unacked_index_present` | gauge | – | `EXISTS unacked_index` | 0 | 1 while the unacked index exists |
| `redis_binding_sets` | gauge | – | `SCAN _kombu.binding.*` count | 3 in the steady hardened stack | drops on reset/queue loss |
| `redis_binding_recreation_events_total` | counter | – | membership additions across binding sets | near 0 in steady state | jumps on Redis reset/restart/topology churn |
| `redis_result_records_total` | gauge | – | `SCAN celery-task-meta-*` count (bounded pages) | small and TTL-bound | collapse beyond TTL behaviour ⇒ alert |
| `redis_acl_denied_commands_total` | counter | `role` ∈ {broker,results,monitor,other}, `category` ∈ {destructive,other} | ACL LOG command denials | 0 in production | any growth is an incident signal |
| `redis_auth_failures_total` | counter | – | ACL LOG `reason=auth` entries | 0 in production | repeated failures are investigated |
| `redis_acl_emergency_use_total` | counter | – | ACL LOG entries for `rtrv-emergency` (command or auth) | 0 | any growth is a critical audit event |
| `redis_acl_default_auth_success_total` | counter | – | `ACL DRYRUN default PING` transition to OK | 0 | growth means the deny-by-default posture regressed |
| `redis_acl_default_usable` | gauge | – | `ACL DRYRUN default PING` | 0 | 1 means the default user is usable |
| `redis_rdb_last_bgsave_status` | gauge | – | `INFO persistence` | 1 (ok) | 0 on persistence failure |

Retrieval limitation (documented): exact unacked counts (`HLEN unacked`,
`ZCARD unacked_index`) are unavailable under the accepted `rtrv-monitor`
allowlist, which intentionally omits those commands. Presence (`redis_unacked_*`)
plus queue depth, key counts, and binding metrics provide the accepted A3
correlation semantics; no ACL change is made to obtain counts.

Counter semantics: ACL LOG counters adopt pre-existing entries without counting
them at exporter start and increment on observed growth; growth survives ACL
LOG ring-buffer aging. Prometheus `increase()` tolerates exporter restarts.

## PostgreSQL collector (`pg-monitor-exporter`, dedicated read-only role)

| Metric | Type | Labels | Source | Healthy baseline | Failure semantics |
|---|---|---|---|---|---|
| `retriva_pg_nonterminal_jobs` | gauge | – | `SELECT count(*) FROM jobs.jobs WHERE status NOT IN ('succeeded','failed','cancelled')` | 0 in the steady stack | last value retained while stale; staleness alert fires |
| `retriva_pg_monitor_up` | gauge | – | collector | 1 | 0 on query failure |
| `retriva_pg_monitor_last_success_timestamp_seconds` | gauge | – | collector | now-15s | stale ⇒ `MonitoringPostgresGaugeStale` |
| `retriva_pg_monitor_query_errors_total` | counter | – | collector | 0 | grows on failures |

Least-privilege grant template (live deployment step; no schema change):

```sql
CREATE ROLE retriva_monitor LOGIN PASSWORD :'monitor_password';
GRANT CONNECT ON DATABASE retriva TO retriva_monitor;
GRANT USAGE ON SCHEMA jobs TO retriva_monitor;
GRANT SELECT ON TABLE jobs.jobs TO retriva_monitor;
```

Statement timeout is enforced via `PGOPTIONS=-c statement_timeout=5000`.

## Alert routing ledger

| Alert class | Alert(s) | Severity | Route |
|---|---|---|---|
| A1 | RedisDeniedDestructiveCommand | critical | monitoring-sink (critical route) |
| A2 | RedisAuthFailuresAboveBaseline | warning | monitoring-sink |
| A3 | RedisTotalKeyCollapse, RedisQueueDisappearance | critical | monitoring-sink (critical route) |
| A4 | RedisBindingRecreationSpike | warning | monitoring-sink |
| A5 | RedisResultRecordCollapse | critical | monitoring-sink (critical route) |
| A6 | RedisEmergencyIdentityUse, RedisDefaultOrNopassAuthSuccess | critical | monitoring-sink (critical route) |
| persistence | RedisRestartOrPersistenceAnomaly | warning | monitoring-sink |
| self | MonitoringCollectorScrapeDown, MonitoringAlertmanagerDown, MonitoringRedisExporterAuthFailures, MonitoringRuleEvaluationFailures, MonitoringPostgresGaugeStale, MonitoringConfigReloadFailed, MonitoringStorageHighUsage | critical/warning | monitoring-sink |
