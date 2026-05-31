from __future__ import annotations

import numpy as np
import pytest

from precip_type_diag.grid import (
    GridDataQualityError,
    GridInputs,
    diagnose_grid_categorical_with_quality,
)


def _inputs() -> GridInputs:
    return GridInputs(
        temperature_k=np.array([[-20.0, -20.0], [-18.0, -18.0], [-16.0, -16.0]]) + 273.15,
        pressure_pa=np.array([[70000.0, 70000.0], [80000.0, 80000.0], [90000.0, 90000.0]]),
        specific_humidity=np.full((3, 2), 0.0015),
        half_level_height_m=np.array([[3000.0, 3000.0], [2000.0, 2000.0], [1000.0, 1000.0], [0.0, 0.0]]),
        total_precip_mm=np.array([1.0, 0.0]),
        ground_temperature_c=np.array([-5.0, -5.0]),
    )


def test_grid_quality_reports_invalid_dry_profile_without_using_it() -> None:
    inputs = _inputs()
    inputs.temperature_k[:, 1] = np.nan

    result = diagnose_grid_categorical_with_quality(inputs)

    assert result.categorical.shape == (2,)
    assert result.quality.active_columns == 1
    assert result.quality.invalid_profile_columns == 1
    assert result.quality.invalid_active_profile_columns == 0


def test_grid_quality_rejects_invalid_active_profile() -> None:
    inputs = _inputs()
    inputs.temperature_k[:, 0] = np.nan

    with pytest.raises(GridDataQualityError, match="active precipitation column"):
        diagnose_grid_categorical_with_quality(inputs)


def test_grid_quality_rejects_non_finite_precipitation() -> None:
    inputs = _inputs()
    inputs.total_precip_mm[1] = np.nan

    with pytest.raises(GridDataQualityError, match="total_precip_mm"):
        diagnose_grid_categorical_with_quality(inputs)
