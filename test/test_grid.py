from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from precip_type_diag.grid import (
    GridDataQualityError,
    GridInputs,
    diagnose_grid,
    diagnose_grid_categorical_with_quality,
    diagnose_grid_probabilities_with_quality,
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


def _icon_vector_inputs() -> tuple[GridInputs, list[int]]:
    document = json.loads((Path(__file__).parent / "data" / "icon_fortran_reference.json").read_text(encoding="utf-8"))
    cases = document["cases"]
    amounts = []
    for case in cases:
        amount = case["total_precip_mm"] - case["previous_total_precip_mm"]
        amounts.append(case["total_precip_mm"] if amount < 0.0 else amount)
    return (
        GridInputs(
            temperature_k=np.asarray([case["temperature_k"] for case in cases]).T,
            pressure_pa=np.asarray([case["pressure_pa"] for case in cases]).T,
            specific_humidity=np.asarray([case["specific_humidity"] for case in cases]).T,
            half_level_height_m=np.asarray([case["half_level_height_m"] for case in cases]).T,
            total_precip_mm=np.asarray(amounts),
            ground_temperature_c=np.asarray([case["ground_temperature_k"] - 273.15 for case in cases]),
            rain_rate_kg_m2_s=np.asarray([case["rain_rate"] for case in cases]),
            snow_rate_kg_m2_s=np.asarray([case["snow_rate"] for case in cases]),
            graupel_rate_kg_m2_s=np.asarray([case["graupel_rate"] for case in cases]),
            hail_rate_kg_m2_s=np.asarray([case["hail_rate"] for case in cases]),
        ),
        [case["expected_code"] for case in cases],
    )


def test_icon_grid_numba_matches_fortran_vectors_and_scalar_reference() -> None:
    inputs, expected = _icon_vector_inputs()
    scalar_codes, scalar_diagnostics = diagnose_grid(inputs, algorithm="icon", precip_mask_threshold_mm=0.01)
    categorical = diagnose_grid_categorical_with_quality(inputs, algorithm="icon", precip_mask_threshold_mm=0.01)
    probabilities = diagnose_grid_probabilities_with_quality(inputs, algorithm="icon", precip_mask_threshold_mm=0.01)

    np.testing.assert_array_equal(scalar_codes, expected)
    np.testing.assert_array_equal(categorical.categorical, expected)
    np.testing.assert_array_equal(probabilities.categorical, expected)
    assert categorical.quality.active_columns == len(expected) - 1
    for index, diagnostic in enumerate(scalar_diagnostics):
        scalar_probabilities = diagnostic.probabilities
        for name in ("rain", "snow", "ice_pellets", "freezing_drizzle", "freezing_rain_on_ground", "freezing_rain"):
            np.testing.assert_allclose(probabilities.probabilities[f"prob_{name}_mm"][index], getattr(scalar_probabilities, name))


def test_default_grid_algorithm_remains_firdewsa() -> None:
    inputs = _inputs()
    default = diagnose_grid_categorical_with_quality(inputs)
    explicit = diagnose_grid_categorical_with_quality(inputs, algorithm="firdewsa")
    np.testing.assert_array_equal(default.categorical, explicit.categorical)


def test_icon_grid_rejects_nonfinite_active_microphysics_rate() -> None:
    inputs, _ = _icon_vector_inputs()
    bad_rate = np.asarray(inputs.rain_rate_kg_m2_s).copy()
    bad_rate[1] = np.nan
    bad_inputs = GridInputs(
        temperature_k=inputs.temperature_k,
        pressure_pa=inputs.pressure_pa,
        specific_humidity=inputs.specific_humidity,
        half_level_height_m=inputs.half_level_height_m,
        total_precip_mm=inputs.total_precip_mm,
        ground_temperature_c=inputs.ground_temperature_c,
        rain_rate_kg_m2_s=bad_rate,
    )
    with pytest.raises(GridDataQualityError, match="microphysics rates"):
        diagnose_grid_categorical_with_quality(bad_inputs, algorithm="icon", precip_mask_threshold_mm=0.01)
