#!/bin/sh
# Spec 034 / ADR-039 — render Redis ACL users from environment secrets and
# start redis-server. Fail-closed: missing or malformed secrets abort startup.
#
# The rendered ACL file lives only at /run/redis-acl/users.acl (mode 0600);
# no credential is ever written to the repository, the image, logs, or Compose.
set -eu

template="${REDIS_ACL_TEMPLATE:-/etc/redis/users.acl.template}"
out_dir="${REDIS_ACL_OUT_DIR:-/run/redis-acl}"
conf="${REDIS_CONF:-/etc/redis/redis.conf}"
out="$out_dir/users.acl"

check_secret() {
    name="$1"
    value="$2"
    if [ -z "$value" ]; then
        echo "redis entrypoint: $name is required" >&2
        exit 1
    fi
    case "$value" in
        *[!A-Za-z0-9_-]*)
            echo "redis entrypoint: $name contains unsupported characters (allowed: A-Z a-z 0-9 _ -)" >&2
            exit 1
            ;;
    esac
}

check_secret REDIS_BROKER_PASSWORD "${REDIS_BROKER_PASSWORD-}"
check_secret REDIS_RESULTS_PASSWORD "${REDIS_RESULTS_PASSWORD-}"
check_secret REDIS_MONITOR_PASSWORD "${REDIS_MONITOR_PASSWORD-}"
check_secret REDIS_HEALTH_PASSWORD "${REDIS_HEALTH_PASSWORD-}"

allow_default="${REDIS_ALLOW_UNAUTHENTICATED_DEFAULT:-no}"
case "$allow_default" in
    yes|no) ;;
    *)
        echo "redis entrypoint: invalid REDIS_ALLOW_UNAUTHENTICATED_DEFAULT" >&2
        exit 1
        ;;
esac

mkdir -p "$out_dir"
umask 077

# Redis ACL files accept only `user ...` directives — no comments, no blank
# lines — so documentation comments in the template are stripped while the
# passwords are injected.
sed \
    -e "s|__REDIS_BROKER_PASSWORD__|${REDIS_BROKER_PASSWORD}|g" \
    -e "s|__REDIS_RESULTS_PASSWORD__|${REDIS_RESULTS_PASSWORD}|g" \
    -e "s|__REDIS_MONITOR_PASSWORD__|${REDIS_MONITOR_PASSWORD}|g" \
    -e "s|__REDIS_HEALTH_PASSWORD__|${REDIS_HEALTH_PASSWORD}|g" \
    "$template" | grep -Ev '^[[:space:]]*(#|$)' > "$out"

if [ "$allow_default" = "yes" ]; then
    # Staged-rollout window only (never the final production posture).
    sed -i 's|^user default off$|user default on nopass ~* \&* +@all|' "$out"
fi

if grep -q '__REDIS_[A-Z_]*_PASSWORD__' "$out"; then
    echo "redis entrypoint: unresolved password placeholder in rendered ACL" >&2
    exit 1
fi

if [ "$allow_default" = "no" ] && ! grep -q '^user default off$' "$out"; then
    echo "redis entrypoint: default user must be off in the final posture" >&2
    exit 1
fi

chmod 700 "$out_dir"
chmod 600 "$out"
chown redis:redis "$out_dir" "$out" 2>/dev/null || true

# Fresh named volumes are root-owned; redis-server drops to the redis user
# (same privilege-drop as the upstream redis:7-alpine entrypoint) and must be
# able to persist RDB files in /data.
find /data ! -user redis -exec chown redis '{}' + 2>/dev/null || true

exec setpriv --reuid redis --regid redis --clear-groups redis-server "$conf"
