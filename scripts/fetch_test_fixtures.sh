#!/usr/bin/env bash

set -euo pipefail

DEST_ROOT="${1:-test/fixtures}"
REMOTE_HOST="${REMOTE_HOST:-tasna}"
REMOTE_CACHE_ROOT="${REMOTE_CACHE_ROOT:-/opr/osm/inn/cache}"

CH1_DEST="${DEST_ROOT}/real_icon_ch1_eps"
CH2_DEST="${DEST_ROOT}/real_icon_ch2_eps"

CH1_FILES=(
  "lfff00000000c"
  "lfff00010000"
)

CH2_FILES=(
  "lfff00000000c"
  "lfff04170000"
  "lfff04180000"
)

have_all_files() {
  local destination="$1"
  shift
  local name
  for name in "$@"; do
    if [[ ! -f "${destination}/${name}" ]]; then
      return 1
    fi
  done
  return 0
}

find_remote_member_dir() {
  local model="$1"
  shift
  local files=("$@")
  local file_args=""
  local file
  for file in "${files[@]}"; do
    file_args+=" $(printf '%q' "$file")"
  done

  ssh "${REMOTE_HOST}" "bash -lc '
set -euo pipefail
cache_root=$(printf %q "${REMOTE_CACHE_ROOT}")
model=$(printf %q "${model}")
base=\"\${cache_root}/\${model}/FCST_RING\"
if [[ ! -d \"\${base}\" ]]; then
  exit 1
fi
for run_dir in \$(find \"\${base}\" -mindepth 1 -maxdepth 1 -type d | sort -r); do
  member_dir=\"\${run_dir}/icon/000\"
  [[ -d \"\${member_dir}\" ]] || continue
  ok=1
  for file in${file_args}; do
    if [[ ! -f \"\${member_dir}/\${file}\" ]]; then
      ok=0
      break
    fi
  done
  if [[ \${ok} -eq 1 ]]; then
    printf \"%s\n\" \"\${member_dir}\"
    exit 0
  fi
done
exit 1
'"
}

fetch_fixture_set() {
  local model="$1"
  local destination="$2"
  shift 2
  local files=("$@")

  mkdir -p "${destination}"

  if have_all_files "${destination}" "${files[@]}"; then
    echo "Skipping ${model}: fixtures already present in ${destination}"
    return 0
  fi

  local remote_member_dir
  remote_member_dir="$(find_remote_member_dir "${model}" "${files[@]}")"

  echo "Fetching ${model} fixtures from ${REMOTE_HOST}:${remote_member_dir}"
  local missing=()
  local file
  for file in "${files[@]}"; do
    if [[ ! -f "${destination}/${file}" ]]; then
      missing+=("${REMOTE_HOST}:${remote_member_dir}/${file}")
    fi
  done

  if [[ "${#missing[@]}" -eq 0 ]]; then
    echo "Skipping ${model}: no missing files after remote discovery"
    return 0
  fi

  scp "${missing[@]}" "${destination}/"
}

fetch_fixture_set "ICON-CH1-EPS" "${CH1_DEST}" "${CH1_FILES[@]}"
fetch_fixture_set "ICON-CH2-EPS" "${CH2_DEST}" "${CH2_FILES[@]}"

echo "Fixture fetch complete under ${DEST_ROOT}"
echo "These files are ignored by git via .gitignore."
