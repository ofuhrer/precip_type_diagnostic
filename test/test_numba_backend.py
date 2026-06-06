from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("numba")

from precip_type_diag.numba_backend import diagnose_column_categorical_numba, diagnose_column_probabilities_numba
from precip_type_diag.profile import ThermodynamicColumn, diagnose_column_from_thermodynamics

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
        (
            thermo(
                [
                    2.191386997143269,
                    1.1784905183637269,
                    0.1655940395841848,
                    -0.8473024391953572,
                    -1.8601989179748992,
                    -2.873095396754442,
                    -3.809654339682999,
                    -4.440863139207622,
                    -5.072071938732244,
                    -5.703280738256867,
                    -6.334489537781489,
                    -6.965698337306111,
                    -7.37139839043624,
                    -7.438835323974628,
                    -7.506272257513015,
                    -7.573709191051403,
                    -7.641146124589791,
                    -7.708583058128179,
                    -6.416811453052377,
                    -4.218900822233778,
                    -2.0209901914151702,
                    0.17692043940343002,
                    2.37483107022203,
                    4.572741701040637,
                    5.231877676754591,
                    5.506319988692382,
                    5.780762300630174,
                    6.055204612567965,
                    6.329646924505756,
                    6.6040892364435475,
                ],
                [90] * 30,
                list(np.linspace(-12, -2, 30)),
            ),
            np.linspace(12000, 0, 30),
            1.0,
            -5.0,
            0.0,
        ),
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

    probability_result = diagnose_column_probabilities_numba(
        thermodynamics.temperature_c,
        thermodynamics.wet_bulb_c,
        thermodynamics.relative_humidity_ice_pct,
        heights,
        total_precip_mm,
        ground_temperature_c,
        threshold_mm,
    )
    assert int(probability_result[0]) == int(expected.categorical_code)
    np.testing.assert_allclose(probability_result[1], expected.microphysics_probabilities.rain, atol=1e-10)
    np.testing.assert_allclose(probability_result[2], expected.microphysics_probabilities.snow, atol=1e-10)
    np.testing.assert_allclose(probability_result[3], expected.microphysics_probabilities.ice_pellets, atol=1e-10)
    np.testing.assert_allclose(probability_result[4], expected.microphysics_probabilities.freezing_drizzle, atol=1e-10)
    np.testing.assert_allclose(probability_result[5], expected.microphysics_probabilities.freezing_rain_on_ground, atol=1e-10)
    np.testing.assert_allclose(probability_result[6], expected.microphysics_probabilities.freezing_rain, atol=1e-10)
    assert np.all(np.isfinite(probability_result[1:]))


def test_numba_probability_backend_matches_original_no_rh_layer_fallback() -> None:
    thermodynamics = thermo(
        [-5.0, -2.0, 1.0, 4.0, 8.0],
        [40.0, 45.0, 50.0, 55.0, 60.0],
        [-20.0, -15.0, -8.0, -2.0, 5.0],
    )
    expected = diagnose_column_from_thermodynamics(
        thermodynamics=thermodynamics,
        full_level_height_m=H5,
        total_precip_mm=1.0,
        ground_temperature_c=4.0,
        precip_mask_threshold_mm=0.0,
    )

    assert int(expected.categorical_code) != 0

    probability_result = diagnose_column_probabilities_numba(
        thermodynamics.temperature_c,
        thermodynamics.wet_bulb_c,
        thermodynamics.relative_humidity_ice_pct,
        H5,
        1.0,
        4.0,
        0.0,
    )

    assert int(probability_result[0]) == int(expected.categorical_code)
    np.testing.assert_allclose(probability_result[1], expected.microphysics_probabilities.rain, atol=1e-10)
