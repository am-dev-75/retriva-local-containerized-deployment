#!/bin/sh
# Spec 034 / ADR-039 — read-only Redis operational helpers (rtrv-monitor role).
#
# This script never executes destructive commands. It exists so operators can
# inspect aggregates and verify the ACL denials without any write capability.
set -eu

: "${REDIS_MONITOR_PASSWORD:?set REDIS_MONITOR_PASSWORD}"
REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"

R() {
    REDISCLI_AUTH="$REDIS_MONITOR_PASSWORD" redis-cli \
        --user rtrv-monitor -h "$REDIS_HOST" -p "$REDIS_PORT" "$@"
}

expect() {
    # expect <allowed|denied> <role> <command...>
    # ACL DRYRUN returns the literal "OK" when permitted and a plain
    # "User <u> has no permissions to ..." string when denied (it is not a
    # RESP error), so the reply text is matched directly.
    want="$1"
    shift
    role="$1"
    shift
    result=$(R ACL DRYRUN "$role" "$@" 2>&1 || true)
    case "$want:$result" in
        denied:*"no permissions"*)
            echo "OK   $role denied: $*"
            ;;
        allowed:OK)
            echo "OK   $role allowed: $*"
            ;;
        *)
            echo "FAIL $role $* -> $result"
            ;;
    esac
}

cmd="${1:-status}"
case "$cmd" in
    status)
        R INFO server | grep -E 'redis_version|uptime_in_seconds' || true
        echo "key_counts:"
        R DBSIZE
        echo "ingestion_queue_depth:"
        R LLEN ingestion 2>/dev/null || echo "(denied or absent)"
        ;;
    acl-review)
        # Safe aggregate: users and rules with password hashes redacted.
        R ACL LIST | sed -E 's/>[^ ]+/>[redacted]/g; s/#[0-9a-f]+/#[redacted]/g'
        ;;
    unacked)
        # Read-only pre/post baseline.  The monitor role intentionally has
        # no ZCARD/ZRANGE on the broker's unacked_index; EXISTS=1 proves a
        # non-empty sorted set (Redis removes empty zsets), and LLEN/DBSIZE
        # capture the queue/keyspace side.  Never grant monitor broker-key
        # reads without a separate least-privilege review.
        echo "unacked_index_exists:"; R EXISTS unacked_index
        echo "unacked_index_type:"; R TYPE unacked_index 2>/dev/null || true
        echo "unacked_index_ttl:"; R TTL unacked_index 2>/dev/null || true
        echo "ingestion_queue_depth:"; R LLEN ingestion 2>/dev/null || echo "(denied or absent)"
        echo "dbsize:"; R DBSIZE
        ;;
    verify-acl)
        # Every denial check uses ACL DRYRUN with an arity-valid argument
        # vector (ACL DRYRUN validates arity before permissions). No
        # destructive command is ever executed.
        for role in rtrv-broker rtrv-results rtrv-monitor rtrv-health; do
            expect denied "$role" flushall
            expect denied "$role" flushdb
            expect denied "$role" swapdb 0 1
            expect denied "$role" keys '*'
            expect denied "$role" config get maxmemory
            expect denied "$role" config set maxmemory 0
            expect denied "$role" shutdown nosave
            expect denied "$role" debug sleep 0
            expect denied "$role" monitor
            expect denied "$role" replicaof no one
            expect denied "$role" slaveof no one
            expect denied "$role" failover
            expect denied "$role" save
            expect denied "$role" bgsave
            expect denied "$role" bgrewriteaof
            expect denied "$role" cluster info
            expect denied "$role" module list
            expect denied "$role" migrate 127.0.0.1 1 k 0 1
            expect denied "$role" restore k 0 v
            expect denied "$role" dump k
            expect denied "$role" script flush
            expect denied "$role" script kill
            expect denied "$role" function list
            expect denied "$role" wait 0 0
            expect denied "$role" client kill 127.0.0.1:1
            expect denied "$role" client pause 1
            expect denied "$role" client unpause
            expect denied "$role" latency reset
        done
        # NOTE: RESET cannot be ACL-restricted (Redis no-auth connection
        # command); its effect is connection-local only. Recorded as an
        # accepted Redis-semantics exception in docs/redis-acl-runbook.md.
        for role in rtrv-results rtrv-health; do
            expect denied "$role" acl list
            expect denied "$role" acl log
        done
        for role in rtrv-results rtrv-monitor rtrv-health; do
            expect denied "$role" eval 'return 1' 0
            expect denied "$role" evalsha 0000000000000000000000000000000000000000 0
            expect denied "$role" script load 'return 1'
        done
        # rtrv-broker is the only identity with scripting: kombu's
        # unacked-restoration mutex releases a redis-py Lock via
        # EVALSHA/SCRIPT LOAD. Redis 7 enforces this user's ACLs for commands
        # invoked inside scripts, so the grant is bounded by the allowlist
        # (see docs/redis-acl-runbook.md). Plain EVAL is never granted.
        expect denied rtrv-broker eval 'return 1' 0
        expect allowed rtrv-broker evalsha 0000000000000000000000000000000000000000 0
        expect allowed rtrv-broker script load 'return 1'
        expect allowed rtrv-broker script exists 0000000000000000000000000000000000000000
        expect allowed rtrv-broker set unacked_mutex probe
        expect allowed rtrv-broker get unacked_mutex
        expect allowed rtrv-broker llen ingestion
        expect allowed rtrv-broker watch unacked
        expect allowed rtrv-broker rpush ingestion probe
        expect allowed rtrv-broker brpop ingestion 1
        expect allowed rtrv-broker time
        expect allowed rtrv-broker exists ingestion
        expect allowed rtrv-broker pexpire ingestion 1000
        expect allowed rtrv-broker sadd _kombu.binding_permanent.ingestion x
        expect allowed rtrv-broker zadd _kombu.binding_deadlines 1 x
        # Celery/Kombu 5.7.0 restore_visible: ZRANGE ... BYSCORE REV.
        expect allowed rtrv-broker zrange unacked_index 0 -1
        # Results-backend commands used by simple ingestion tasks.
        expect allowed rtrv-results setex celery-task-meta-probe 60 x
        expect allowed rtrv-results get celery-task-meta-probe
        expect allowed rtrv-results expire celery-task-meta-probe 60
        expect allowed rtrv-results del celery-task-meta-probe
        expect denied rtrv-monitor acl setuser probe
        expect denied rtrv-monitor acl deluser probe
        expect denied rtrv-monitor config set maxmemory 0
        echo "cross-role key-pattern isolation:"
        expect denied rtrv-broker brpop celery-task-meta-probe 1
        expect denied rtrv-broker del celery-task-meta-probe
        expect denied rtrv-broker rpush result:probe 1
        expect denied rtrv-results set ingestion probe
        expect denied rtrv-results rpush ingestion probe
        expect denied rtrv-monitor set ingestion probe
        expect denied rtrv-health set ingestion probe
        echo "required permissions:"
        expect allowed rtrv-broker brpop ingestion 1
        expect allowed rtrv-broker lpush ingestion probe
        expect allowed rtrv-results set celery-task-meta-probe x
        expect allowed rtrv-monitor info
        expect allowed rtrv-monitor acl list
        expect allowed rtrv-monitor acl log
        expect allowed rtrv-monitor acl dryrun rtrv-broker get
        expect allowed rtrv-health ping
        ;;
    *)
        echo "usage: redis-ops.sh status|acl-review|verify-acl" >&2
        exit 2
        ;;
esac
