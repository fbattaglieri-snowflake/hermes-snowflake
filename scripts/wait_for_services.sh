#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for service in "$@"; do
  deadline=$((SECONDS + 600))
  until snow spcs service list-containers "$service" -x --format JSON --silent | python3 "$script_dir/check_containers.py"; do
    if (( SECONDS >= deadline )); then
      echo "Service readiness deadline exceeded: $service" >&2
      exit 1
    fi
    sleep 10
  done
  echo "All reported containers READY: $service"
done