#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  tools/run_depl_cycle.sh MODEL YYYYMMDD HH OUTPUT_ROOT [extra precip_type_diag args...]

Runs one explicit DEPL production cycle in the Balfrin realtime FDB environment.

Examples:
  tools/run_depl_cycle.sh ICON-CH2-EPS 20260531 18 /users/$USER/work/ptype-fdb
  tools/run_depl_cycle.sh ICON-CH1-EPS 20260531 1800 /users/$USER/work/ptype-fdb --max-wall-s 3600

The script does not submit to SLURM and does not select a queue. Use an open
service-node partition such as pp-short in the scheduler layer when applicable.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 4 ]]; then
  usage >&2
  exit 2
fi

MODEL="$1"
DATE="$2"
TIME_VALUE="$3"
OUTPUT_ROOT="$4"
shift 4

case "$MODEL" in
  ICON-CH1-EPS|ICON-CH2-EPS) ;;
  *)
    echo "Unsupported MODEL '$MODEL'; expected ICON-CH1-EPS or ICON-CH2-EPS" >&2
    exit 2
    ;;
esac

if [[ ! "$DATE" =~ ^[0-9]{8}$ ]]; then
  echo "DATE must use YYYYMMDD, got '$DATE'" >&2
  exit 2
fi

if [[ "$TIME_VALUE" =~ ^[0-9]{2}$ ]]; then
  TIME_VALUE="${TIME_VALUE}00"
elif [[ ! "$TIME_VALUE" =~ ^[0-9]{4}$ ]]; then
  echo "HH must use HH or HHMM, got '$TIME_VALUE'" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UENV_BIN="${PRECIP_TYPE_DIAG_UENV:-/usr/bin/uenv}"
PYTHON_BIN="${PRECIP_TYPE_DIAG_PYTHON:-$REPO_ROOT/.venv-fdb-5.21/bin/python}"
FDB_IMAGE="${PRECIP_TYPE_DIAG_FDB_IMAGE:-fdb/5.21:v1}"
FDB_SITE_PACKAGES="${PRECIP_TYPE_DIAG_FDB_SITE_PACKAGES:-/user-environment/venvs/fdb/lib/python3.11/site-packages}"
RUN_ID="${PRECIP_TYPE_DIAG_RUN_ID:-${MODEL}-${DATE}-${TIME_VALUE}}"
ATTEMPT="${PRECIP_TYPE_DIAG_ATTEMPT:-1}"

cd "$REPO_ROOT"

exec "$UENV_BIN" run --view=realtime "$FDB_IMAGE" -- \
  env PYTHONPATH="${FDB_SITE_PACKAGES}:src${PYTHONPATH:+:${PYTHONPATH}}" \
  "$PYTHON_BIN" -m precip_type_diag \
  --model "$MODEL" \
  --date "$DATE" \
  --time "$TIME_VALUE" \
  --members all \
  --workers 8 \
  --chunk-size 2 \
  --output-format netcdf \
  --write-probability-products \
  --output-root "$OUTPUT_ROOT" \
  --log-level INFO \
  --log-format json \
  --run-id "$RUN_ID" \
  --attempt "$ATTEMPT" \
  --fdb-retries 3 \
  --fdb-retry-initial-s 10 \
  --fdb-retry-max-s 120 \
  "$@"
