from __future__ import annotations

import numpy as np

from precip_type_diag.constants import PrecipitationTypeCode
from precip_type_diag.profile import (
    ThermodynamicColumn,
    calculate_thermodynamics,
    diagnose_column_from_thermodynamics,
)


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


def test_wet_bulb_temperature_calculation_stability() -> None:
    temperature_k = np.array([263.15, 273.15, 278.15])
    specific_humidity = np.array([0.0015, 0.0035, 0.0060])
    pressure_pa = np.array([70000.0, 85000.0, 95000.0])

    result = calculate_thermodynamics(temperature_k, specific_humidity, pressure_pa)

    np.testing.assert_allclose(result.temperature_c, [-10.0, 0.0, 5.0], atol=1e-10)
    np.testing.assert_allclose(result.wet_bulb_c, [-11.66723349, -1.30330555, 5.34851860], atol=1e-8)
    np.testing.assert_allclose(result.relative_humidity_ice_pct, [54.71357185, 75.81552405, 105.80420823], atol=1e-8)


def test_all_subfreezing_profile_gives_snow() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-20, -18, -16, -14, -12], [90, 90, 90, 90, 90]),
        H5,
        total_precip_mm=1.0,
        ground_temperature_c=-5.0,
    )
    assert diagnostics.categorical_code == PrecipitationTypeCode.SNOW


def test_above_freezing_profile_gives_rain() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-8, 1, 3, 4, 5], [90, 90, 90, 90, 90]),
        H5,
        total_precip_mm=1.0,
        ground_temperature_c=2.0,
    )
    assert diagnostics.categorical_code == PrecipitationTypeCode.RAIN


def test_elevated_warm_nose_over_shallow_cold_layer_gives_freezing_rain() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-8, 2, 4, 1, -1], [90, 90, 90, 90, 90]),
        H5,
        total_precip_mm=1.0,
        ground_temperature_c=-1.0,
    )
    assert diagnostics.categorical_code == PrecipitationTypeCode.FREEZING_RAIN


def test_elevated_warm_nose_over_deep_cold_layer_gives_ice_pellets() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-20, -10, 2, 4, 0, -4, -8], [95, 95, 95, 95, 95, 95, 95]),
        H7,
        total_precip_mm=1.0,
        ground_temperature_c=-8.0,
    )
    assert diagnostics.categorical_code == PrecipitationTypeCode.ICE_PELLETS


def test_shallow_subfreezing_low_cloud_warm_rain_case_gives_freezing_drizzle() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-6, -5, -4, -3, -2], [50, 60, 70, 80, 82]),
        H5,
        total_precip_mm=1.0,
        ground_temperature_c=-4.0,
    )
    assert diagnostics.categorical_code == PrecipitationTypeCode.FREEZING_DRIZZLE


def test_no_precipitation_masks_output_to_zero() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-20, -18, -16, -14, -12], [90, 90, 90, 90, 90]),
        H5,
        total_precip_mm=0.0,
        ground_temperature_c=-5.0,
    )
    assert diagnostics.categorical_code == PrecipitationTypeCode.NO_PRECIP
    assert diagnostics.microphysics_probabilities.total() == 0.0


def test_precipitation_mask_threshold_masks_light_precipitation() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-8, 2, 4, 1, -1], [90, 90, 90, 90, 90]),
        H5,
        total_precip_mm=0.05,
        ground_temperature_c=-1.0,
        precip_mask_threshold_mm=0.1,
    )
    assert diagnostics.categorical_code == PrecipitationTypeCode.NO_PRECIP
    assert diagnostics.microphysics_probabilities.total() == 0.0


def test_multiple_precipitation_generation_layers_use_highest_prob_ice() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo(
            [-20, -18, -16, -14, -12, -10, -8, -6],
            [95, 95, 95, 60, 95, 95, 95, 95],
        ),
        H8,
        total_precip_mm=1.0,
        ground_temperature_c=-5.0,
    )
    assert diagnostics.pure_prob_ice == 100.0
    assert diagnostics.categorical_code == PrecipitationTypeCode.SNOW


def test_small_area_suppression_keeps_merged_rain_solution() -> None:
    with_small_area = diagnose_column_from_thermodynamics(
        thermo([-20, 2, -0.01, 4, 5, 6, 7, 8], [95] * 8),
        H8,
        total_precip_mm=1.0,
        ground_temperature_c=5.0,
    )
    without_small_area = diagnose_column_from_thermodynamics(
        thermo([-20, 2, 4, 5, 6, 7, 8], [95] * 7),
        H7,
        total_precip_mm=1.0,
        ground_temperature_c=5.0,
    )
    assert with_small_area.categorical_code == PrecipitationTypeCode.RAIN
    assert with_small_area.categorical_code == without_small_area.categorical_code


def test_freezing_rain_on_ground_threshold_is_minus_three_celsius() -> None:
    below_threshold = diagnose_column_from_thermodynamics(
        thermo([-8, 2, 4, 5, 6], [90, 90, 90, 90, 90]),
        H5,
        total_precip_mm=1.0,
        ground_temperature_c=-4.0,
    )
    above_threshold = diagnose_column_from_thermodynamics(
        thermo([-8, 2, 4, 5, 6], [90, 90, 90, 90, 90]),
        H5,
        total_precip_mm=1.0,
        ground_temperature_c=-2.0,
    )
    assert below_threshold.categorical_code == PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND
    assert above_threshold.categorical_code == PrecipitationTypeCode.RAIN


def test_microphysics_fallback_reclassifies_columns_with_precipitation() -> None:
    diagnostics = diagnose_column_from_thermodynamics(
        thermo([-20, -18, -16, -14, -12], [50, 80, 50, 50, 50]),
        H5,
        total_precip_mm=1.0,
        ground_temperature_c=-5.0,
    )
    assert diagnostics.pure_probabilities.total() == 0.0
    assert diagnostics.microphysics_prob_ice == 100.0
    assert diagnostics.categorical_code == PrecipitationTypeCode.SNOW


def test_categorical_code_stability() -> None:
    assert int(PrecipitationTypeCode.NO_PRECIP) == 0
    assert int(PrecipitationTypeCode.RAIN) == 1
    assert int(PrecipitationTypeCode.FREEZING_RAIN) == 3
    assert int(PrecipitationTypeCode.SNOW) == 5
    assert int(PrecipitationTypeCode.ICE_PELLETS) == 8
    assert int(PrecipitationTypeCode.FREEZING_DRIZZLE) == 12
    assert int(PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND) == 13
