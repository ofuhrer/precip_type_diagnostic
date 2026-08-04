#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  tools/run_depl_cycle.sh MODEL YYYYMMDD HH OUTPUT_ROOT [progressive options...]

Processes every newly complete hour of one explicit DEPL cycle. Repeated calls
resume from CYCLE.json and preserve already published NetCDF probabilities.

Examples:
  tools/run_depl_cycle.sh ICON-CH2-EPS 20260531 18 /users/$USER/work/ptype-fdb
  tools/run_depl_cycle.sh ICON-CH1-EPS 20260531 1800 /users/$USER/work/ptype-fdb --through-step 2

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
exec "$SCRIPT_DIR/run_balfrin.sh" realtime "$MODEL" "$OUTPUT_ROOT" \
  --date "$DATE" --time "$TIME_VALUE" "$@"
