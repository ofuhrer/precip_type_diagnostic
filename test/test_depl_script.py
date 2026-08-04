from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "run_depl_cycle.sh"


def test_depl_script_help_is_available() -> None:
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "Usage:" in result.stdout
    assert "pp-short" in result.stdout


def test_depl_script_uses_explicit_production_options() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'run_balfrin.sh" realtime' in text
    assert '--date "$DATE" --time "$TIME_VALUE"' in text
    assert "#SBATCH" not in text
    assert "pp-production" not in text


def test_unified_balfrin_runner_documents_all_production_modes() -> None:
    runner = REPO_ROOT / "tools" / "run_balfrin.sh"
    result = subprocess.run([str(runner), "--help"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "realtime MODEL OUTPUT_ROOT" in result.stdout
    assert "cycle MODEL" in result.stdout
    assert "backfill-plan" in result.stdout
    assert "backfill-task" in result.stdout
    assert "backfill-status" in result.stdout


def test_unified_balfrin_runner_accepts_uenv_backed_python_symlink() -> None:
    text = (REPO_ROOT / "tools" / "run_balfrin.sh").read_text(encoding="utf-8")

    assert '[[ ! -x "$PYTHON_BIN" && ! -L "$PYTHON_BIN" ]]' in text


def test_balfrin_setup_pins_reviewed_runtime() -> None:
    text = (REPO_ROOT / "tools" / "setup_balfrin.sh").read_text(encoding="utf-8")

    assert "fdb/5.21:v1" in text
    assert 'numba==0.66.0' in text
    assert 'llvmlite==0.48.0' in text
    assert "pip check" in text
