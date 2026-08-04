from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from precip_type_diag.constants import ICON_REFERENCE_COMMIT
from precip_type_diag.icon_numba_backend import diagnose_icon_column_probabilities_numba
from precip_type_diag.icon_profile import (
    KELVIN_OFFSET,
    IconColumnProfile,
    SurfacePrecipitationRates,
    diagnose_icon_column,
    diagnose_icon_column_from_thermodynamics,
    icon_precip_amount_threshold,
)
from precip_type_diag.profile import ThermodynamicColumn

REFERENCE_VECTORS = Path(__file__).parent / "data" / "icon_fortran_reference.json"


def test_icon_scalar_matches_frozen_fortran_reference_vectors() -> None:
    document = json.loads(REFERENCE_VECTORS.read_text(encoding="utf-8"))
    assert document["source_commit"] == ICON_REFERENCE_COMMIT
    assert document["source_sha256"] == "74426609a7954059991bda8c610a87026b40e8bf2053d6471914f0f5c3fbbc9d"
    threshold = icon_precip_amount_threshold(float(document["interval_seconds"]))
    assert threshold == document["precip_amount_threshold_mm"]

    actual: dict[str, int] = {}
    for case in document["cases"]:
        precip_amount = case["total_precip_mm"] - case["previous_total_precip_mm"]
        if precip_amount < 0.0:
            precip_amount = case["total_precip_mm"]
        diagnostics = diagnose_icon_column(
            IconColumnProfile(
                temperature_k=np.asarray(case["temperature_k"]),
                pressure_pa=np.asarray(case["pressure_pa"]),
                specific_humidity=np.asarray(case["specific_humidity"]),
                full_level_height_m=np.asarray(case["full_level_height_m"]),
                half_level_height_m=np.asarray(case["half_level_height_m"]),
                total_precip_mm=precip_amount,
                ground_temperature_c=case["ground_temperature_k"] - KELVIN_OFFSET,
                surface_rates=SurfacePrecipitationRates(
                    rain_kg_m2_s=case["rain_rate"],
                    snow_kg_m2_s=case["snow_rate"],
                    graupel_kg_m2_s=case["graupel_rate"],
                    hail_kg_m2_s=case["hail_rate"],
                ),
            ),
            precip_mask_threshold_mm=threshold,
        )
        actual[case["name"]] = int(diagnostics.categorical_code)
        assert actual[case["name"]] == case["expected_code"]

    assert set(actual.values()) == {0, 1, 3, 5, 6, 7, 8, 9, 10, 12, 13}


def test_icon_trace_threshold_scales_with_interval() -> None:
    assert icon_precip_amount_threshold(0.0) == 1.0e-6
    assert icon_precip_amount_threshold(1800.0) == 0.005
    assert icon_precip_amount_threshold(3600.0) == 0.01
    assert icon_precip_amount_threshold(7200.0) == 0.02


def test_icon_numba_matches_scalar_reference_for_randomized_profiles() -> None:
    random = np.random.default_rng(83429)
    for _ in range(100):
        nlev = 16
        half_level_height_m = np.r_[np.cumsum(random.uniform(300.0, 1100.0, nlev))[::-1], 0.0]
        full_level_height_m = 0.5 * (half_level_height_m[:-1] + half_level_height_m[1:])
        thermodynamics = ThermodynamicColumn(
            temperature_c=random.uniform(-25.0, 8.0, nlev),
            wet_bulb_c=random.uniform(-12.0, 7.0, nlev),
            relative_humidity_ice_pct=random.uniform(40.0, 120.0, nlev),
        )
        rates = SurfacePrecipitationRates(*random.uniform(0.0, 8.0e-5, 4))
        ground_temperature_c = float(random.uniform(-10.0, 5.0))
        scalar = diagnose_icon_column_from_thermodynamics(
            thermodynamics,
            full_level_height_m,
            half_level_height_m,
            1.0,
            ground_temperature_c,
            surface_rates=rates,
        )
        accelerated = diagnose_icon_column_probabilities_numba(
            thermodynamics.temperature_c,
            thermodynamics.wet_bulb_c,
            thermodynamics.relative_humidity_ice_pct,
            full_level_height_m,
            half_level_height_m,
            1.0,
            ground_temperature_c,
            rates.rain_kg_m2_s,
            rates.snow_kg_m2_s,
            rates.graupel_kg_m2_s,
            rates.hail_kg_m2_s,
            0.01,
            12000.0,
        )
        assert accelerated[0] == int(scalar.categorical_code)
        expected_probabilities = (
            scalar.probabilities.rain,
            scalar.probabilities.snow,
            scalar.probabilities.ice_pellets,
            scalar.probabilities.freezing_drizzle,
            scalar.probabilities.freezing_rain_on_ground,
            scalar.probabilities.freezing_rain,
        )
        np.testing.assert_allclose(accelerated[1:], expected_probabilities, rtol=1.0e-11, atol=1.0e-11)
