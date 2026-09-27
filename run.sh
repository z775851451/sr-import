#!/bin/zsh
# Scope Recall conversation backfill.
#
#   ./run.sh plan                     read only
#   ./run.sh check --limit 400        predict what the store will accept
#   ./run.sh register-entries         idempotent
#   ./run.sh run --limit 30           bounded batch
#   ./run.sh run                      incremental backfill of everything new
#   ./run.sh status
set -euo pipefail

PY="${SR_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$DIR"
exec "$PY" -m sr_import.cli "$@"
