#!/bin/sh
# Read-only PostgreSQL aggregate collector for the canonical Retriva monitoring
# stack (Spec 035 / ADR-040).
#
# Exposes ONLY aggregate counts through a tiny static HTTP endpoint:
#   retriva_pg_nonterminal_jobs
#   retriva_pg_monitor_up
#   retriva_pg_monitor_last_success_timestamp_seconds
#   retriva_pg_monitor_query_errors_total
#
# Security: dedicated least-privilege read-only role; single indexed aggregate
# query; statement timeout; counts only; never exports tenant/job/attempt/task
# identifiers, content, or errors. Password comes from the environment
# (accepted secret interface) and never appears in argv or output.
set -eu

: "${PGHOST:?PGHOST required}"
: "${PGUSER:?PGUSER required}"
: "${PGPASSWORD:?PGPASSWORD required}"
: "${PGDATABASE:?PGDATABASE required}"
PGPORT="${PGPORT:-5432}"
METRICS_PORT="${METRICS_PORT:-9188}"
POLL_INTERVAL="${PG_POLL_INTERVAL_SECONDS:-15}"
METRICS_DIR="${METRICS_DIR:-/metrics}"
export PGOPTIONS="${PGOPTIONS:--c statement_timeout=5000}"

mkdir -p "$METRICS_DIR"
errors=0

collect() {
    timestamp="$(date +%s)"
    if count="$(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
        -Atc "SELECT count(*) FROM jobs.jobs WHERE status NOT IN ('succeeded','failed','cancelled');" 2>/dev/null)"; then
        up=1
        last_success="$timestamp"
    else
        up=0
        errors=$((errors + 1))
        count=0
        last_success="$(cat "$METRICS_DIR/.last_success" 2>/dev/null || echo 0)"
    fi
    printf '%s' "$last_success" > "$METRICS_DIR/.last_success"
    tmp="$METRICS_DIR/.metrics.$$"
    {
        echo "# HELP retriva_pg_nonterminal_jobs Durable jobs not in a terminal state (aggregate only)"
        echo "# TYPE retriva_pg_nonterminal_jobs gauge"
        echo "retriva_pg_nonterminal_jobs ${count:-0}"
        echo "# HELP retriva_pg_monitor_up 1 when the last aggregate query succeeded"
        echo "# TYPE retriva_pg_monitor_up gauge"
        echo "retriva_pg_monitor_up $up"
        echo "# HELP retriva_pg_monitor_last_success_timestamp_seconds Unix time of the last successful query"
        echo "# TYPE retriva_pg_monitor_last_success_timestamp_seconds gauge"
        echo "retriva_pg_monitor_last_success_timestamp_seconds $last_success"
        echo "# HELP retriva_pg_monitor_query_errors_total Aggregate query failures"
        echo "# TYPE retriva_pg_monitor_query_errors_total counter"
        echo "retriva_pg_monitor_query_errors_total $errors"
    } > "$tmp"
    mv "$tmp" "$METRICS_DIR/metrics"
}

# collect once before serving so the endpoint is valid immediately
collect
(
    while true; do
        sleep "$POLL_INTERVAL"
        collect
    done
) &
exec busybox httpd -f -p "$METRICS_PORT" -h "$METRICS_DIR"
