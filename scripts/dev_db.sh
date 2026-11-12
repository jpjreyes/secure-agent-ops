#!/usr/bin/env bash
# Postgres for the LangGraph checkpointer, on Colima (never Docker Desktop).
#   scripts/dev_db.sh up | down | status
# down stops only this database; Colima keeps running (other projects use it).
set -euo pipefail
cd "$(dirname "$0")/.."
export DOCKER_HOST="unix://${HOME}/.colima/default/docker.sock"
COMPOSE=(docker compose -p sao-p1 -f infra/postgres/compose.yaml)

case "${1:-status}" in
  up) "${COMPOSE[@]}" up -d --wait ;;
  down) "${COMPOSE[@]}" stop ;;
  status) "${COMPOSE[@]}" ps ;;
  *) echo "usage: $0 up|down|status"; exit 1 ;;
esac
