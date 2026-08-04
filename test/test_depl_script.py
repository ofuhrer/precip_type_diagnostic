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

    assert "--members all" in text
    assert "--workers 8" in text
    assert "--chunk-size 2" in text
    assert "--output-format netcdf" in text
    assert "--write-probability-products" in text
    assert "--log-format json" in text
    assert "--fdb-retries 3" in text
    assert "#SBATCH" not in text
    assert "pp-production" not in text
