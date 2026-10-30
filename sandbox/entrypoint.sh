#!/bin/sh
# Wait for the evaluation's dind daemon, then run the container's command (by default it just
# stays up for `docker exec`).
set -eu

timeout="${OPENWEIGHT_DOCKER_WAIT:-60}"
waited=0
until docker info >/dev/null 2>&1; do
  if [ "$waited" -ge "$timeout" ]; then
    echo "entrypoint: no Docker daemon at ${DOCKER_HOST:-default} after ${timeout}s" >&2
    break
  fi
  sleep 1
  waited=$((waited + 1))
done

exec "$@"
