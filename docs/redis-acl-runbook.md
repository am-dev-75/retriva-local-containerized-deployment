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

- The worker's late-acknowledgement restore path needs the broker identity to
  run `WATCH`/`MULTI`/`EXEC` on `unacked`, `LLEN`, `RPUSH`, `HSET`/`HGET`/
  `HDEL`, `ZADD`/`ZREM`/`ZREVRANGEBYSCORE` on `unacked_index`, plus the
  unacked-restoration mutex (`SET`/`GET`/`DEL` on `unacked_mutex`).
- Kombu's mutex releases a redis-py `Lock`, which requires `EVALSHA` +
  `SCRIPT LOAD` with the redis-py release script
  (sha1 `c3f8721cbb97f72bc19e972846bd7aaf91901658`). These are granted to
  `rtrv-broker` only, and only because Redis 7 enforces the calling user's
  ACLs for commands invoked inside Lua scripts: a loaded script cannot exceed
  the broker allowlist (verified by attempting an in-script `FLUSHALL`, which
  is blocked). Plain `EVAL` and `SCRIPT FLUSH|KILL` remain denied for all
  identities. `rtrv-results`, `rtrv-monitor`, and `rtrv-health` have no
  scripting.
- Queue keys include kombu priority variants (`ingestion*`), the pidbox
  mailboxes and reply queues (`*.celery.pidbox*`, `celeryev.*`), and the
  `_kombu.binding.*` exchange sets; fanout channels are `/0.`-prefixed under
  database 0.
- Redis `PSUBSCRIBE` requires the requested pattern to be granted exactly:
  the worker's event receiver subscribes with `/0.celeryev/worker.*`, so that
  literal channel pattern is in the broker allowlist (a broader
  `/0.celeryev/*` alone is not sufficient for it).
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
