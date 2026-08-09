#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  tools/run_balfrin.sh realtime MODEL OUTPUT_ROOT [progressive options...]
  tools/run_balfrin.sh cycle MODEL YYYYMMDD HH|HHMM OUTPUT_ROOT [diagnostic options...]
  tools/run_balfrin.sh backfill-plan [backfill plan options...]
  tools/run_balfrin.sh backfill-task MANIFEST INDEX [task options...]
  tools/run_balfrin.sh backfill-status MANIFEST [--verify-outputs]
  tools/run_balfrin.sh analysis-plan [analysis plan options...]
  tools/run_balfrin.sh analysis-task MANIFEST INDEX
  tools/run_balfrin.sh analysis-reduce MANIFEST
  tools/run_balfrin.sh analysis-status MANIFEST [--verify-outputs]
  tools/run_balfrin.sh analysis-retire-source MANIFEST --confirm-source-root PATH [--delete-source]
  tools/run_balfrin.sh regional-mask [regional build-mask options...]
  tools/run_balfrin.sh regional-plan [regional plan options...]
  tools/run_balfrin.sh regional-task MANIFEST INDEX
  tools/run_balfrin.sh regional-reduce MANIFEST
  tools/run_balfrin.sh regional-status MANIFEST [--verify-outputs]

Accepted production defaults:
  algorithm: Firdewsa
  realtime:  all members, progressive NetCDF diagnostics and probabilities
  REA:       member 000, daily steps 1..24, atomic monthly GRIB2 archives
USAGE
}

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

MODE="$1"
shift
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UENV_BIN="${PRECIP_TYPE_DIAG_UENV:-/usr/bin/uenv}"
PYTHON_BIN="${PRECIP_TYPE_DIAG_PYTHON:-$REPO_ROOT/.venv-fdb-5.21/bin/python}"
FDB_IMAGE="${PRECIP_TYPE_DIAG_FDB_IMAGE:-fdb/5.21:v1}"
FDB_SITE_PACKAGES="${PRECIP_TYPE_DIAG_FDB_SITE_PACKAGES:-/user-environment/venvs/fdb/lib/python3.11/site-packages}"

if [[ ! -x "$PYTHON_BIN" && ! -L "$PYTHON_BIN" ]]; then
  echo "Balfrin environment is missing at $PYTHON_BIN; run tools/setup_balfrin.sh first" >&2
  exit 1
fi

run_in_view() {
  local view="$1"
  shift
  cd "$REPO_ROOT"
  exec "$UENV_BIN" run --view="$view" "$FDB_IMAGE" -- \
    env PYTHONPATH="$FDB_SITE_PACKAGES:$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" "$@"
}

normalize_time() {
  local value="$1"
  if [[ "$value" =~ ^[0-9]{2}$ ]]; then
    printf '%s00' "$value"
  elif [[ "$value" =~ ^[0-9]{4}$ ]]; then
    printf '%s' "$value"
  else
    echo "time must use HH or HHMM, got '$value'" >&2
    return 2
  fi
}

case "$MODE" in
  realtime)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    MODEL="$1"
    OUTPUT_ROOT="$2"
    shift 2
    case "$MODEL" in ICON-CH1-EPS|ICON-CH2-EPS) ;; *) echo "unsupported realtime model: $MODEL" >&2; exit 2 ;; esac
    run_in_view realtime -m precip_type_diag.realtime --model "$MODEL" --output-root "$OUTPUT_ROOT" "$@"
    ;;
  cycle)
    [[ $# -ge 4 ]] || { usage >&2; exit 2; }
    MODEL="$1"
    DATE="$2"
    TIME_VALUE="$(normalize_time "$3")"
    OUTPUT_ROOT="$4"
    shift 4
    case "$MODEL" in
      ICON-CH1-EPS|ICON-CH2-EPS)
        run_in_view realtime -m precip_type_diag --model "$MODEL" --date "$DATE" --time "$TIME_VALUE" \
          --members all --output-format netcdf --write-probability-products --output-root "$OUTPUT_ROOT" \
          --workers 8 --chunk-size 2 --fdb-retries 3 --log-level INFO --log-format json "$@"
        ;;
      ICON-REA-L-CH1)
        [[ "$TIME_VALUE" == "0000" ]] || { echo "ICON-REA-L-CH1 requires time 0000" >&2; exit 2; }
        run_in_view rea-l-ch1 -m precip_type_diag --model "$MODEL" --date "$DATE" --time 0000 \
          --members 000 --output-format grib2 --output-root "$OUTPUT_ROOT" --workers 1 --chunk-size 2 \
          --fdb-retries 3 --log-level INFO --log-format json "$@"
        ;;
      *) echo "unsupported model: $MODEL" >&2; exit 2 ;;
    esac
    ;;
  backfill-plan)
    run_in_view rea-l-ch1 -m precip_type_diag.backfill plan "$@"
    ;;
  backfill-task)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    INDEX="$2"
    shift 2
    run_in_view rea-l-ch1 -m precip_type_diag.backfill run-task --manifest "$MANIFEST" --index "$INDEX" "$@"
    ;;
  backfill-status)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    shift
    run_in_view rea-l-ch1 -m precip_type_diag.backfill status --manifest "$MANIFEST" "$@"
    ;;
  analysis-plan)
    run_in_view rea-l-ch1 -m precip_type_diag.analysis plan "$@"
    ;;
  analysis-task)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    INDEX="$2"
    shift 2
    run_in_view rea-l-ch1 -m precip_type_diag.analysis run-task --manifest "$MANIFEST" --index "$INDEX" "$@"
    ;;
  analysis-reduce)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    shift
    run_in_view rea-l-ch1 -m precip_type_diag.analysis reduce --manifest "$MANIFEST" "$@"
    ;;
  analysis-status)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    shift
    run_in_view rea-l-ch1 -m precip_type_diag.analysis status --manifest "$MANIFEST" "$@"
    ;;
  analysis-retire-source)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    shift
    run_in_view rea-l-ch1 -m precip_type_diag.analysis retire-source --manifest "$MANIFEST" "$@"
    ;;
  regional-mask)
    run_in_view rea-l-ch1 -m precip_type_diag.regional_analysis build-mask "$@"
    ;;
  regional-plan)
    run_in_view rea-l-ch1 -m precip_type_diag.regional_analysis plan "$@"
    ;;
  regional-task)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    INDEX="$2"
    shift 2
    run_in_view rea-l-ch1 -m precip_type_diag.regional_analysis run-task --manifest "$MANIFEST" --index "$INDEX" "$@"
    ;;
  regional-reduce)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    shift
    run_in_view rea-l-ch1 -m precip_type_diag.regional_analysis reduce --manifest "$MANIFEST" "$@"
    ;;
  regional-status)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    MANIFEST="$1"
    shift
    run_in_view rea-l-ch1 -m precip_type_diag.regional_analysis status --manifest "$MANIFEST" "$@"
    ;;
  *)
    echo "unknown mode: $MODE" >&2
    usage >&2
    exit 2
    ;;
esac
