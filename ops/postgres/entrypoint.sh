#!/bin/sh
set -eu

docker-entrypoint.sh postgres &
pid=$!

trap 'kill -TERM "$pid"; wait "$pid"' TERM INT

while ! psql -U translator -d translator -tAc "SELECT 1 FROM pg_database WHERE datname = 'langfuse_v4'" | grep -q 1; do
  if ! kill -0 "$pid" 2>/dev/null; then
    wait "$pid"
  fi
  psql -U translator -d translator -v ON_ERROR_STOP=1 -c "CREATE DATABASE langfuse_v4 OWNER translator" || true
  sleep 1
done

wait "$pid"
