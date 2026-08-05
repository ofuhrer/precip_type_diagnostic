#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: tools/submit_backfill_campaign.sh --manifest PATH [backfill-plan options...]

Submit an inventory planner on pp-short. After a strict plan succeeds, the
planner job submits the generated monthly pp-long array. Re-running the same
command resumes an interrupted yearly inventory checkpoint.
USAGE
}

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST=""
MANIFEST_INDEX=-1
MANIFEST_EQUALS=false
SLURM_SCRIPT=""
SLURM_INDEX=-1
SLURM_EQUALS=false
ARGS=("$@")
for ((index = 0; index < ${#ARGS[@]}; index++)); do
  case "${ARGS[$index]}" in
    --manifest)
      ((index + 1 < ${#ARGS[@]})) || { echo "--manifest requires a path" >&2; exit 2; }
      MANIFEST_INDEX=$index
      MANIFEST="${ARGS[$((index + 1))]}"
      ;;
    --manifest=*)
      MANIFEST_INDEX=$index
      MANIFEST_EQUALS=true
      MANIFEST="${ARGS[$index]#--manifest=}"
      ;;
    --slurm-script)
      ((index + 1 < ${#ARGS[@]})) || { echo "--slurm-script requires a path" >&2; exit 2; }
      SLURM_INDEX=$index
      SLURM_SCRIPT="${ARGS[$((index + 1))]}"
      ;;
    --slurm-script=*)
      SLURM_INDEX=$index
      SLURM_EQUALS=true
      SLURM_SCRIPT="${ARGS[$index]#--slurm-script=}"
      ;;
  esac
done
[[ -n "$MANIFEST" ]] || { echo "--manifest is required" >&2; usage >&2; exit 2; }

[[ "$MANIFEST" == /* ]] || MANIFEST="$PWD/$MANIFEST"
MANIFEST_PARENT="$(dirname "$MANIFEST")"
mkdir -p "$MANIFEST_PARENT"
MANIFEST_DIR="$(cd "$MANIFEST_PARENT" && pwd)"
MANIFEST="$MANIFEST_DIR/$(basename "$MANIFEST")"
if $MANIFEST_EQUALS; then
  ARGS[$MANIFEST_INDEX]="--manifest=$MANIFEST"
else
  ARGS[$((MANIFEST_INDEX + 1))]="$MANIFEST"
fi

if [[ -z "$SLURM_SCRIPT" ]]; then
  SLURM_SCRIPT="${MANIFEST%.*}.sbatch"
else
  [[ "$SLURM_SCRIPT" == /* ]] || SLURM_SCRIPT="$PWD/$SLURM_SCRIPT"
  SLURM_PARENT="$(dirname "$SLURM_SCRIPT")"
  mkdir -p "$SLURM_PARENT"
  SLURM_PARENT="$(cd "$SLURM_PARENT" && pwd)"
  SLURM_SCRIPT="$SLURM_PARENT/$(basename "$SLURM_SCRIPT")"
  if $SLURM_EQUALS; then
    ARGS[$SLURM_INDEX]="--slurm-script=$SLURM_SCRIPT"
  else
    ARGS[$((SLURM_INDEX + 1))]="$SLURM_SCRIPT"
  fi
fi

printf -v PLAN_COMMAND '%q ' "$REPO_ROOT/tools/run_balfrin.sh" backfill-plan "${ARGS[@]}"
printf -v ARRAY_COMMAND 'sbatch --parsable %q' "$SLURM_SCRIPT"
WRAPPED_COMMAND="set -eu; cd $(printf '%q' "$REPO_ROOT"); ${PLAN_COMMAND}; ${ARRAY_COMMAND}"

exec sbatch --parsable \
  --partition=pp-short \
  --time=00:59:00 \
  --ntasks=1 \
  --cpus-per-task=8 \
  --job-name=ptype-rea-plan \
  --output="$MANIFEST_DIR/planner-%j.out" \
  --chdir="$REPO_ROOT" \
  --wrap="$WRAPPED_COMMAND"
