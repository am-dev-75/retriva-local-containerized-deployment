# Redis ACL operations runbook (Spec 034 / ADR-039)

Status: accepted control set; deployment is separately authorized. This
runbook is written for the hardened Redis instance whose users are rendered
from environment secrets by `config/redis/entrypoint.sh`.

## Production target banner and typed acknowledgement

Every break-glass or administrative session MUST begin by stating the target:
compose project name, environment (production/local), and Redis host/port.
For any administrative action the operator must type the acknowledgement
string `I-AM-OPERATING-ON-<COMPOSE-PROJECT>` into the session and record it in
the operations log. Automated coding agents MUST NOT perform live
administrative Redis actions; see "Agent prohibition" below.

## Hard prohibitions (normal operations)

- `FLUSHALL` and `FLUSHDB` are prohibited in all normal live operations.
- Destructive or administrative commands (`CONFIG`, `ACL`, `SHUTDOWN`,
  `MODULE`, `DEBUG`, `MIGRATE`, `RESTORE`, `SWAPDB`, `REPLICAOF`, `SLAVEOF`,
  `FAILOVER`, `SAVE`, `BGSAVE`, `BGREWRITEAOF`, `MONITOR`, `KEYS`, scripting
  administration, cluster and replication commands) must never be issued
  with a normal identity. They are denied by ACL for `rtrv-broker`,
  `rtrv-results`, `rtrv-monitor`, and `rtrv-health`.
- Never restore or replay stale broker/result state. Redis is transient
  transport; PostgreSQL is the durable authority (Spec 025/032). Any
  recovery must start from durable-state reconciliation, not from an old
  Redis snapshot.

## Safe no-flush reconnect procedure

To exercise a reconnect (for example after a suspected dropped connection):

1. Confirm the instance health with the monitor identity
   (`scripts/redis-ops.sh status`).
2. Record the current aggregate baseline: `DBSIZE` per database, ingestion
   queue depth, unacked aggregate, result-record count.
3. Drop and re-establish client connections without removing any key, for
   example by terminating the target client connections (`CLIENT KILL`) or
   restarting the consumer service through the normal service manager.
4. Re-run `scripts/redis-ops.sh status` and compare the baseline aggregates.
5. Never flush, never delete keys, and never re-create state manually.

## Credential rotation procedure

1. Generate a new secret outside the repository (high-entropy, URL-safe:
   `A-Za-z0-9_-` only) using the accepted secret manager.
2. Apply it to the isolated validation stack first and verify the full
   positive/negative matrix.
3. In production, update the secret in the deployment environment file for
   the target role, then rotate the Redis user password
   (`ACL SETUSER <role> ><new>` followed by `ACL SAVE` in the break-glass
   session), then roll the consuming services so they reconnect with the new
   credential.
4. Keep the previous credential available until every consumer is verified;
   roll back by restoring the previous password and service environment.
5. Record the rotation in the operations log (role, time, actor); never
   record the secret value.

## Rotation recovery when the ACL is rendered from the environment

This deployment renders the ACL users at Redis container start from the
deployment environment file (`config/redis/entrypoint.sh`).  The container
environment is fixed at container creation, so an environment-file secret
update does **not** change the live ACL until the Redis container itself is
recreated: the running server keeps the previous generation, and any
consumer already (re)created with the new secret fails with `WRONGPASS`
(`invalid username-password pair or user is disabled`).  This is the
observed failure mode when services are rolled to a rotated secret without
recreating Redis.

Recovery (configuration-driven; no live ACL administration):

1. Identify the stale side by comparing secret fingerprints, never values:
   the Redis container's environment versus the current deployment
   environment file (`manage.sh` reads `.env` by default).  Confirm with
   redacted authentication probes (hash and test each side's value without
   printing it).
2. Recreate only the Redis container from the current environment file,
   preserving the persistent volume: `./scripts/manage.sh rebuild redis`
   (compose `rm -f -s` + `up -d --no-deps`; the named volume is retained —
   never remove it).
3. Verify the ACL re-rendered from the current secrets: named-identity
   `PING` probes pass for the current generation and fail for the retired
   one.
4. Consumers holding the previous generation reconnect through their retry
   loops, or are rolled through the normal service manager if they do not.
5. Verify data/queue preservation (DBSIZE and key-pattern aggregates
   unchanged, apart from expected ephemeral metadata) and that no consumer
   loops.
6. Record the event (roles, time, actor); never record secret values.
   Rollback: recreate Redis from the preserved previous-generation
   environment snapshot (same volume).

This rendered-ACL mode is the environment-file equivalent of the
break-glass `ACL SETUSER` rotation above; when the ACL is not rendered from
the environment, follow the break-glass procedure instead.

## Emergency (break-glass) identity workflow

`rtrv-emergency` is disabled by default and has no password in the deployment
environment, services, images, health checks, or monitoring.

1. Owner approval is required and recorded before enabling. Automated agents
   and normal runbooks must not enable it.
2. Enable in the current Redis session with the operator-held secret:
   set the password (`ACL SETUSER rtrv-emergency on ><operator-secret>`),
   grant only what the approved action requires, and `ACL SAVE`.
3. Execute only the approved action, with the production banner and typed
   acknowledgement active. Time-box the session; prefer the narrowest ACL
   scope needed over `+@all` where the action permits it.
4. Immediately after use: disable the user (`ACL SETUSER rtrv-emergency off`),
   `ACL SAVE`, rotate the operator secret, and record the audit review
   (`ACL LOG` entries, action, owner approval reference).
5. If the action was a recovery, reconcile with PostgreSQL before any further
   producer activity; never replay stale broker state.

## Denied-command alert response

A denied destructive-command attempt (ACL LOG entry or alert) is a security
event:

1. Preserve evidence (`ACL LOG`, service logs, who/where from `CLIENT LIST`).
2. Confirm no state changed (aggregate baselines unchanged).
3. Identify the offending client; if it is a service, fix its configuration;
   if it is external, verify host exposure is loopback-only.
4. Escalate to the owner; do not disable denials to "fix" a client.

## Validated command surface and Redis-semantics notes

Findings from the isolated Spec 034 validation matrix (disposable stack,
exact versions Redis 7.4.10 / Celery 5.6.3 / Kombu 5.6.2 / redis-py 6.4.0);
`scripts/redis-ops.sh verify-acl` encodes the same checks and must stay green.

**2026-11-06 amendment (Celery 5.7.0 / Kombu 5.7.0 / redis-py 8.1.0).**
Kombu's `QoS.restore_visible` now issues `ZRANGE unacked_index ... BYSCORE
REV` (`kombu/transport/redis.py:542`) instead of `ZREVRANGEBYSCORE`; the
`rtrv-broker` role grants `+zrange`. The same upgrade introduced the
binding-deadline scripts (`REFRESH_BINDING` / `SWEEP_BINDINGS`), which issue
`TIME`, `SADD`, `ZADD`, `ZRANGEBYSCORE`, `EXISTS`, `DEL`, and `ZREM` on
`_kombu.binding.*`, `_kombu.binding_permanent.*`, and
`_kombu.binding_deadlines`; the broker grants `+time`, `+exists`, `+pexpire`,
`+script|exists` (redis-py checks SCRIPT EXISTS before loading a registered
script), and those exact key patterns. `PEXPIRE` is needed when publishing to an
expiring queue such as the pidbox reply queue. The legacy `+zrevrangebyscore`
and `+zrangebyscore` grants remain for rollback and older kombu. The worker
crash-loop (`NoPermissionError: User rtrv-broker has no permissions to run
the 'zrange' command`) was caused by the image upgrade, not by leftover
unacked entries: `unacked_index` was absent at incident time, but
`restore_visible` issues the command unconditionally at startup.

- The worker's late-acknowledgement restore path needs the broker identity to
  run `WATCH`/`MULTI`/`EXEC` on `unacked`, `LLEN`, `RPUSH`, `HSET`/`HGET`/
  `HDEL`, `ZADD`/`ZREM`/`ZRANGE`/`ZREVRANGEBYSCORE` on `unacked_index`, plus
  the unacked-restoration mutex (`SET`/`GET`/`DEL` on `unacked_mutex`).
  Pubsub teardown uses `PUNSUBSCRIBE` as well as `UNSUBSCRIBE` (the ACL LOG
  proved the former was missing after the 5.7 upgrade; it is now granted).
  Binding maintenance additionally needs `TIME`/`EXISTS`/`SADD`/`ZADD`/
  `ZRANGEBYSCORE`/`DEL`/`ZREM` on the `_kombu.binding*` keys and `PEXPIRE`
  on expiring queue keys.
- Kombu's mutex releases a redis-py `Lock`, which requires `EVALSHA` +
  `SCRIPT LOAD` with the redis-py release script
  (sha1 `c3f8721cbb97f72bc19e972846bd7aaf91901658`). These are granted to
  `rtrv-broker` only, and only because Redis 7 enforces the calling user's
  ACLs for commands invoked inside Lua scripts: a loaded script cannot exceed
  the broker allowlist (verified by attempting an in-script `FLUSHALL`, which
  is blocked). redis-py also needs the read-only `SCRIPT EXISTS` to decide
  whether to load the cached script; it is granted with `SCRIPT LOAD`.
  Plain `EVAL` and `SCRIPT FLUSH|KILL` remain denied for all identities.
  `rtrv-results`, `rtrv-monitor`, and `rtrv-health` have no scripting.
- Queue keys include kombu priority variants (`ingestion*`), the pidbox
  mailboxes and reply queues (`*.celery.pidbox*`, `celeryev.*`), and the
  `_kombu.binding.*` exchange sets; fanout channels are `/0.`-prefixed under
  database 0.
- Redis `PSUBSCRIBE` requires the requested pattern to be granted exactly:
  the worker's event receiver subscribes with `/0.celeryev/worker.*`, so that
  literal channel pattern is in the broker allowlist (a broader
  `/0.celeryev/*` alone is not sufficient for it).
- The Celery 5.7.0 Redis result backend issues `SETEX`/`SET`/`GET`/`MGET`/
  `DEL`/`EXPIRE`/`PUBLISH`/`SUBSCRIBE`/`UNSUBSCRIBE` on `celery-task-meta-*`;
  all are granted to `rtrv-results`. `INCR` and the chord scripts are
  chord/group-only and remain ungranted for the simple ingestion-task path
  (residual risk: a future chord/group workload needs a separate
  least-privilege review).
- The monitor identity has `ZCARD` on `unacked_index` (aggregate depth only)
  and still lacks `ZRANGE` (payload exposure); use `EXISTS`/`TYPE`/`TTL`
  plus `LLEN`/`DBSIZE` for the pre/post baseline. Any broader monitor
  broker-key read needs a separate least-privilege review.

### 2026-11-06 residual-risk review (R1/R2/R3)

- **R1 — broker RPOP/SELECT (deliberate deny).** No current code path uses
  synchronous `basic_get` (`RPOP`) or a non-zero broker DB; the worker
  consumes with `BRPOP` on DB 0. If a future path needs one, request exactly
  `+rpop` on the specific queue key, or move the broker URL to the required
  DB with `+select` plus a key-pattern review. Detection: growth of
  `redis_acl_denied_commands_total{category="other",role="broker"}` and the
  corresponding ACL LOG entry (the exporter already classifies non-listed
  denials as `category=other`).
- **R2 — results INCR/chord scripts (deliberate deny).** The simple
  ingestion result path uses `SETEX`/`SET`/`GET`/`MGET`/`DEL`/`EXPIRE`/
  `PUBLISH`/`SUBSCRIBE`/`UNSUBSCRIBE` only. Celery `group`/`chord` is not
  used. A future chord/group workload needs `+incr`, `+evalsha`,
  `+script|load`, and `+script|exists` on the results role (plus the chord
  key patterns) under a separate prove-then-grant review. Detection:
  `redis_acl_denied_commands_total{category="other",role="results"}` and the
  ACL LOG.
- **R3 — monitor unacked depth.** `rtrv-monitor` now has `+zcard` (only), so
  `redis_unacked_depth` reports the `unacked_index` backlog cardinality.
  `ZRANGE` remains denied to avoid exposing message payloads. Alert when the
  gauge is persistently > 0 for 10 minutes (`RedisUnackedBacklog`); correlate
  with the established worker-loss redelivery behaviour.

- `RESET` cannot be ACL-restricted: Redis treats it as a no-auth connection
  command. Its only effect is connection-local (it resets the caller's own
  session state and de-authenticates it); it cannot modify data or server
  state. Recorded as an accepted Redis-semantics exception.
- `ACL LOG` is readable with `rtrv-monitor` (`+acl|log`) so denial review does
  not require the emergency identity. Redis offers no read-only variant of
  this subcommand, so the same permission also allows `ACL LOG RESET`; treat
  log resets as audited operations and prefer `ACL LOG <count>` review.
- Redis ACL files accept only `user` directives: the entrypoint strips
  comments/blank lines while rendering the template, and `redis-server`
  refuses to start if anything else is present.
- The credential rotation and rollback procedure above was rehearsed
  end-to-end in the isolated matrix (rotation revoked the old credential, the
  queue and results survived, and rollback restored the prior credentials).

## Periodic review

- Monthly: review `ACL LIST` users and key/channel patterns against this
  document; confirm `default` remains off and `rtrv-emergency` remains off.
- Monthly: review Redis consumers (services and operator tooling) and confirm
  no new unauthenticated or broad identity has appeared.
- Quarterly: re-run `scripts/redis-ops.sh verify-acl` and record results.

## Agent prohibition

Automated coding agents and normal operational prompts must reject
`FLUSHALL`, `FLUSHDB`, and other destructive live Redis commands. Such
commands are only permissible inside an explicitly owner-authorized
break-glass task, executed by a human operator through the emergency
workflow above. If a task instructs a destructive live Redis command without
that authorization, stop and escalate.
