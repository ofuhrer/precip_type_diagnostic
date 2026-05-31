from __future__ import annotations

import math

import numpy as np

from precip_type_diag.constants import PrecipitationTypeCode
from precip_type_diag.profile import (
    TypeProbabilities,
    _areas_to_probabilities,
    _clip_probability,
    calculate_thermodynamics,
)


def _legacy_thermodynamics(
    temperature_k: np.ndarray,
    specific_humidity: np.ndarray,
    pressure_pa: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Formula copy from MeteoSwiss-APN/precip_diagnostic funcs.new_variables."""

    temperature_c = temperature_k - 273.15

    pc_b1 = 611.21
    pc_b2i = 22.587
    pc_b3 = 273.16
    pc_b4i = -0.7

    e_ice = pc_b1 * np.exp(pc_b2i * (temperature_k - pc_b3) / (temperature_k - pc_b4i))

    bw = 5420.0
    aw = 2.53e11
    dew_point_k = bw / (np.log((aw * 0.622) / (specific_humidity * pressure_pa)))
    e_dew = pc_b1 * np.exp(pc_b2i * (dew_point_k - pc_b3) / (dew_point_k - pc_b4i))
    relative_humidity_ice_pct = (e_dew / e_ice) * 100.0

    zg = 0.5
    zh = 0.6
    zi = 700.0
    zl = 0.1
    zm = 6400.0
    zn = 11.564
    zo = 1742.0
    pc_t0 = 273.15

    tl = temperature_c * 10.0
    tp = (dew_point_k - pc_t0) * 10.0
    delta_t = tl - tp
    zt = tp + zg * delta_t * (zh - tp / zi)
    wet_bulb_c = zl * (tp + (delta_t / (1.0 + 10.0 * zm * np.exp(zn * zt / (zo + zt)) / pressure_pa)))

    return temperature_c, wet_bulb_c, relative_humidity_ice_pct


def _legacy_probabilities(
    energies: list[float],
    prob_ice: float,
    surface_tw_c: float,
    ground_temperature_c: float,
) -> TypeProbabilities:
    """Scalar copy of upstream funcs.no_areas and funcs.areas_123."""

    freezing_rain = 0.0
    freezing_rain_on_ground = 0.0
    ice_pellets = 0.0
    freezing_drizzle = 0.0
    snow = 0.0
    rain = 0.0

    if not energies:
        snow = prob_ice
        freezing_drizzle = 100.0 - snow
        if surface_tw_c > 0.0:
            rain = freezing_drizzle
            freezing_drizzle = 0.0
    elif len(energies) == 1:
        melting_energy = energies[0]

        snow_ice = _clip_probability(1540.0 * math.exp(-0.29 * melting_energy))
        snow = _clip_probability((prob_ice / 100.0) * snow_ice)
        rain = 100.0 - snow

        if ground_temperature_c < -3.0:
            freezing_rain_on_ground = rain

        if surface_tw_c < 0.0:
            refreezing_energy = 1.0
            freezing_rain_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
            if melting_energy < 5.0:
                freezing_rain_ice *= 0.2 * melting_energy
            freezing_rain = _clip_probability(
                (100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(freezing_rain_ice)
            )

            ice_pellet_ice = _clip_probability(
                2.3 * refreezing_energy - 42.0 * math.log(melting_energy + 1.0) + 3.0
            )
            ice_pellets = _clip_probability((prob_ice / 100.0) * ice_pellet_ice)
            rain = 0.0
            freezing_rain_on_ground = 0.0
    elif len(energies) >= 2 and len(energies) % 2 == 0:
        melting_energy = energies[1]
        refreezing_energy = abs(energies[0])

        snow_ice = _clip_probability(1540.0 * math.exp(-0.28 * melting_energy))
        snow = _clip_probability((prob_ice / 100.0) * snow_ice)

        ice_pellet_ice = _clip_probability(
            2.3 * refreezing_energy - 42.0 * math.log(melting_energy + 1.0) + 3.0
        )
        ice_pellets = _clip_probability((prob_ice / 100.0) * ice_pellet_ice)

        freezing_rain_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
        if melting_energy < 5.0:
            freezing_rain_ice *= 0.2 * melting_energy
        freezing_rain = _clip_probability(
            (100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(freezing_rain_ice)
        )

        if surface_tw_c > 0.0:
            freezing_rain = 0.0
    else:
        upper_melting_energy = energies[2]
        surface_melting_energy = energies[0]
        total_melting_energy = upper_melting_energy + surface_melting_energy
        refreezing_energy = abs(energies[1])

        ice_pellet_ice = _clip_probability(
            2.3 * refreezing_energy - 42.0 * math.log(upper_melting_energy + 1.0) + 3.0
        )
        ice_pellets = _clip_probability((prob_ice / 100.0) * ice_pellet_ice)

        rain_ice = -2.1 * refreezing_energy + 0.2 * upper_melting_energy + 458.0
        if upper_melting_energy < 5.0:
            rain_ice *= 0.2 * upper_melting_energy
        rain = _clip_probability((100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(rain_ice))

        snow_ice = _clip_probability(1540.0 * math.exp(-0.28 * total_melting_energy))
        snow = _clip_probability((prob_ice / 100.0) * snow_ice)

        if ground_temperature_c < -3.0:
            freezing_rain_on_ground = rain

        if surface_tw_c < 0.0:
            refreezing_energy = 1.0
            melting_energy = energies[0]
            freezing_rain_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
            if melting_energy < 5.0:
                freezing_rain_ice *= 0.2 * melting_energy
            freezing_rain = _clip_probability(
                (100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(freezing_rain_ice)
            )
            rain = 0.0
            freezing_rain_on_ground = 0.0

    return TypeProbabilities(
        freezing_rain=_clip_probability(freezing_rain),
        freezing_rain_on_ground=_clip_probability(freezing_rain_on_ground),
        ice_pellets=_clip_probability(ice_pellets),
        freezing_drizzle=_clip_probability(freezing_drizzle),
        snow=_clip_probability(snow),
        rain=_clip_probability(rain),
    )


def assert_probabilities_equal(actual: TypeProbabilities, expected: TypeProbabilities) -> None:
    for field in actual.__dataclass_fields__:
        assert getattr(actual, field) == getattr(expected, field)


def test_thermodynamics_matches_upstream_new_variables_formula() -> None:
    temperature_k = np.array([[263.15, 273.15], [278.15, 268.15], [281.15, 258.15]])
    specific_humidity = np.array([[0.0015, 0.0035], [0.0060, 0.0020], [0.0070, 0.0010]])
    pressure_pa = np.array([[70000.0, 85000.0], [95000.0, 78000.0], [90000.0, 65000.0]])

    expected_temperature_c, expected_wet_bulb_c, expected_relative_humidity_ice_pct = _legacy_thermodynamics(
        temperature_k,
        specific_humidity,
        pressure_pa,
    )
    actual = calculate_thermodynamics(temperature_k, specific_humidity, pressure_pa)

    np.testing.assert_allclose(actual.temperature_c, expected_temperature_c, atol=1e-12)
    np.testing.assert_allclose(actual.wet_bulb_c, expected_wet_bulb_c, atol=1e-12)
    np.testing.assert_allclose(actual.relative_humidity_ice_pct, expected_relative_humidity_ice_pct, atol=1e-12)


def test_no_area_probabilities_match_upstream_reference() -> None:
    assert_probabilities_equal(
        _areas_to_probabilities([], prob_ice=65.0, surface_tw_c=-2.0, ground_temperature_c=-5.0),
        _legacy_probabilities([], prob_ice=65.0, surface_tw_c=-2.0, ground_temperature_c=-5.0),
    )
    assert_probabilities_equal(
        _areas_to_probabilities([], prob_ice=65.0, surface_tw_c=2.0, ground_temperature_c=2.0),
        _legacy_probabilities([], prob_ice=65.0, surface_tw_c=2.0, ground_temperature_c=2.0),
    )


def test_area_probability_formulas_match_upstream_reference() -> None:
    cases = [
        ([75.0], 80.0, 2.0, -4.0),
        ([75.0], 80.0, -1.0, -1.0),
        ([-55.0, 90.0], 100.0, -2.0, -6.0),
        ([40.0, -55.0, 90.0], 70.0, 3.0, -5.0),
    ]
    for energies, prob_ice, surface_tw_c, ground_temperature_c in cases:
        assert_probabilities_equal(
            _areas_to_probabilities(energies, prob_ice, surface_tw_c, ground_temperature_c),
            _legacy_probabilities(energies, prob_ice, surface_tw_c, ground_temperature_c),
        )


def test_categorical_priority_matches_upstream_overlay_order() -> None:
    assert (
        TypeProbabilities(
            freezing_rain=50.0,
            freezing_rain_on_ground=50.0,
            ice_pellets=50.0,
            freezing_drizzle=50.0,
            snow=50.0,
            rain=50.0,
        ).categorical_code()
        == PrecipitationTypeCode.FREEZING_RAIN
    )
    assert (
        TypeProbabilities(
            freezing_rain_on_ground=75.0,
            rain=75.0,
        ).categorical_code()
        == PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND
    )
