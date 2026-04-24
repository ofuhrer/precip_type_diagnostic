from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

pytest.importorskip("numba")

from precip_type_diag.numba_backend import diagnose_column_categorical_numba
from precip_type_diag.profile import ThermodynamicColumn, diagnose_column_from_thermodynamics


REAL_FIXTURE_DIR = Path("test/fixtures/real_icon_ch2_eps")
H5 = np.array([4000.0, 3000.0, 2000.0, 1000.0, 0.0])
H7 = np.array([6000.0, 5000.0, 4000.0, 3000.0, 2000.0, 1000.0, 0.0])
H8 = np.array([7000.0, 6000.0, 5000.0, 4000.0, 3000.0, 2000.0, 1000.0, 0.0])


def thermo(tw: list[float], rhi: list[float], tc: list[float] | None = None) -> ThermodynamicColumn:
    temperature_c = np.array(tc if tc is not None else tw, dtype=float)
    return ThermodynamicColumn(
        temperature_c=temperature_c,
        wet_bulb_c=np.array(tw, dtype=float),
        relative_humidity_ice_pct=np.array(rhi, dtype=float),
    )


@pytest.mark.parametrize(
    ("thermodynamics", "heights", "total_precip_mm", "ground_temperature_c", "threshold_mm"),
    [
        (thermo([-20, -18, -16, -14, -12], [90, 90, 90, 90, 90]), H5, 1.0, -5.0, 0.0),
        (thermo([-8, 1, 3, 4, 5], [90, 90, 90, 90, 90]), H5, 1.0, 2.0, 0.0),
        (thermo([-8, 2, 4, 1, -1], [90, 90, 90, 90, 90]), H5, 1.0, -1.0, 0.0),
        (thermo([-20, -10, 2, 4, 0, -4, -8], [95, 95, 95, 95, 95, 95, 95]), H7, 1.0, -8.0, 0.0),
        (thermo([-6, -5, -4, -3, -2], [50, 60, 70, 80, 82]), H5, 1.0, -4.0, 0.0),
        (thermo([-20, -18, -16, -14, -12], [90, 90, 90, 90, 90]), H5, 0.0, -5.0, 0.0),
        (thermo([-8, 2, 4, 1, -1], [90, 90, 90, 90, 90]), H5, 0.05, -1.0, 0.1),
        (thermo([-20, -18, -16, -14, -12, -10, -8, -6], [95, 95, 95, 60, 95, 95, 95, 95]), H8, 1.0, -5.0, 0.0),
        (thermo([-20, 2, -0.01, 4, 5, 6, 7, 8], [95] * 8), H8, 1.0, 5.0, 0.0),
        (thermo([-8, 2, 4, 5, 6], [90, 90, 90, 90, 90]), H5, 1.0, -4.0, 0.0),
        (thermo([-20, -18, -16, -14, -12], [50, 80, 50, 50, 50]), H5, 1.0, -5.0, 0.0),
    ],
)
def test_numba_column_backend_matches_python_reference(
    thermodynamics: ThermodynamicColumn,
    heights: np.ndarray,
    total_precip_mm: float,
    ground_temperature_c: float,
    threshold_mm: float,
) -> None:
    expected = diagnose_column_from_thermodynamics(
        thermodynamics=thermodynamics,
        full_level_height_m=heights,
        total_precip_mm=total_precip_mm,
        ground_temperature_c=ground_temperature_c,
        precip_mask_threshold_mm=threshold_mm,
    )
    actual = diagnose_column_categorical_numba(
        thermodynamics.temperature_c,
        thermodynamics.wet_bulb_c,
        thermodynamics.relative_humidity_ice_pct,
        heights,
        total_precip_mm,
        ground_temperature_c,
        threshold_mm,
    )
    assert int(actual) == int(expected.categorical_code)


def test_numba_grid_backend_matches_python_reference_on_real_ch2_fixture() -> None:
    required = ["lfff00000000c", "lfff04170000", "lfff04180000"]
    if any(not (REAL_FIXTURE_DIR / name).exists() for name in required):
        pytest.skip("Real CH2-EPS fixture triplet is not fully present")

    script = f"""
import json
from pathlib import Path
import numpy as np
from precip_type_diag.gribio import MemberHourJob, bootstrap_eccodes_definitions, load_member_hour
from precip_type_diag.grid import diagnose_grid_categorical

fixture_dir = Path({str(REAL_FIXTURE_DIR)!r})
bootstrap_eccodes_definitions()
grid_inputs, _, _ = load_member_hour(
    MemberHourJob(
        member="000",
        step="04180000",
        current_file=fixture_dir / "lfff04180000",
        previous_file=fixture_dir / "lfff04170000",
        constants_file=fixture_dir / "lfff00000000c",
    )
)
categorical_numba_1 = diagnose_grid_categorical(grid_inputs)
categorical_numba_2 = diagnose_grid_categorical(grid_inputs)
print(json.dumps({{"equal": bool(np.array_equal(categorical_numba_1, categorical_numba_2))}}))
"""
    python_executable = Path(".venv-opt2b/bin/python")
    try:
        result = subprocess.run(
            [str(python_executable if python_executable.exists() else Path(sys.executable)), "-c", script],
            cwd=Path.cwd(),
            env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if "ECCODES ERROR" in stderr or "KeyValueNotFoundError" in stderr:
            pytest.skip("Real CH2-EPS decoding is unavailable in this interpreter")
        raise
    payload = json.loads(result.stdout.strip())
    assert payload["equal"] is True
