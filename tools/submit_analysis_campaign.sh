#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: tools/submit_analysis_campaign.sh --manifest PATH [analysis-plan options...]

Build an immutable analysis manifest, submit the monthly pp-long array, and
submit its reducer with an afterok dependency. Re-running is safe: completed
monthly tasks validate and reuse their published products.
USAGE
}

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST=""
ARRAY_SCRIPT=""
REDUCE_SCRIPT=""
ARGS=("$@")
for ((index = 0; index < ${#ARGS[@]}; index++)); do
  case "${ARGS[$index]}" in
    --manifest)
      ((index + 1 < ${#ARGS[@]})) || { echo "--manifest requires a path" >&2; exit 2; }
      MANIFEST="${ARGS[$((index + 1))]}"
      ;;
    --manifest=*) MANIFEST="${ARGS[$index]#--manifest=}" ;;
    --slurm-script)
      ((index + 1 < ${#ARGS[@]})) || { echo "--slurm-script requires a path" >&2; exit 2; }
      ARRAY_SCRIPT="${ARGS[$((index + 1))]}"
      ;;
    --slurm-script=*) ARRAY_SCRIPT="${ARGS[$index]#--slurm-script=}" ;;
    --reduce-slurm-script)
      ((index + 1 < ${#ARGS[@]})) || { echo "--reduce-slurm-script requires a path" >&2; exit 2; }
      REDUCE_SCRIPT="${ARGS[$((index + 1))]}"
      ;;
    --reduce-slurm-script=*) REDUCE_SCRIPT="${ARGS[$index]#--reduce-slurm-script=}" ;;
  esac
done
[[ -n "$MANIFEST" ]] || { echo "--manifest is required" >&2; usage >&2; exit 2; }

"$REPO_ROOT/tools/run_balfrin.sh" analysis-plan "${ARGS[@]}"
if [[ "$MANIFEST" != /* ]]; then MANIFEST="$PWD/$MANIFEST"; fi
MANIFEST="$(cd "$(dirname "$MANIFEST")" && pwd)/$(basename "$MANIFEST")"
if [[ -z "$ARRAY_SCRIPT" ]]; then ARRAY_SCRIPT="${MANIFEST%.*}.sbatch"; fi
if [[ -z "$REDUCE_SCRIPT" ]]; then REDUCE_SCRIPT="${MANIFEST%.*}.reduce.sbatch"; fi
if [[ "$ARRAY_SCRIPT" != /* ]]; then ARRAY_SCRIPT="$PWD/$ARRAY_SCRIPT"; fi
if [[ "$REDUCE_SCRIPT" != /* ]]; then REDUCE_SCRIPT="$PWD/$REDUCE_SCRIPT"; fi
ARRAY_JOB_ID="$(sbatch --parsable "$ARRAY_SCRIPT")"
REDUCE_JOB_ID="$(sbatch --parsable --dependency="afterok:$ARRAY_JOB_ID" "$REDUCE_SCRIPT")"
printf '{"array_job_id":"%s","reducer_job_id":"%s","manifest":"%s"}\n' "$ARRAY_JOB_ID" "$REDUCE_JOB_ID" "$MANIFEST"
