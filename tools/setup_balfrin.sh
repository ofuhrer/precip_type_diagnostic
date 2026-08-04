#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
UENV_BIN="${PRECIP_TYPE_DIAG_UENV:-/usr/bin/uenv}"
FDB_IMAGE="${PRECIP_TYPE_DIAG_FDB_IMAGE:-fdb/5.21:v1}"
VENV_DIR="${PRECIP_TYPE_DIAG_VENV:-$REPO_ROOT/.venv-fdb-5.21}"
FDB_SITE_PACKAGES="${PRECIP_TYPE_DIAG_FDB_SITE_PACKAGES:-/user-environment/venvs/fdb/lib/python3.11/site-packages}"

if [[ ! -x "$UENV_BIN" ]]; then
  echo "uenv client is unavailable at $UENV_BIN" >&2
  exit 1
fi

cd "$REPO_ROOT"
"$UENV_BIN" --version
"$UENV_BIN" image ls "$FDB_IMAGE"

"$UENV_BIN" run --view=realtime "$FDB_IMAGE" -- bash -lc '
  set -euo pipefail
  python -m venv "$1"
  "$1/bin/python" -m pip install "numba==0.66.0" "llvmlite==0.48.0"
  "$1/bin/python" -m pip install --no-deps -e "$2"
  env PYTHONPATH="$3" "$1/bin/python" -m pip check
  env PYTHONPATH="$3:$2/src" "$1/bin/python" -c "import eccodes; import earthkit.data; import netCDF4; import numba; import precip_type_diag; print(\"Balfrin runtime ready\")"
' bash "$VENV_DIR" "$REPO_ROOT" "$FDB_SITE_PACKAGES"

echo "Setup complete. Run: tools/run_balfrin.sh --help"
