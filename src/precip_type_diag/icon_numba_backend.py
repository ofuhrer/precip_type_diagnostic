"""Numba-accelerated backend for the ICON-adapted diagnostic."""

from __future__ import annotations

import numba as _numba
import numpy as np

from .icon_profile import (
    FREEZING_RAIN_INTERCEPT,
    FREEZING_RAIN_LOW_MELT_THRESHOLD_JKG,
    FREEZING_RAIN_MELT_COEFF,
    FREEZING_RAIN_REFREEZE_COEFF,
    GRAVITY,
    GROUND_FREEZING_THRESHOLD_C,
    ICE_PELLET_SURFACE_MELT_KEEP_MAX_JKG,
    ICE_PELLET_SURFACE_MELT_RAIN_MIN_JKG,
    KELVIN_OFFSET,
    LARGE_REFREEZING_SNOW_THRESHOLD_JKG,
    PRECIP_GENERATION_MIN_DEPTH_M,
    PRECIP_GENERATION_RHI_THRESHOLD_PCT,
    PROB_ICE_FULL_THRESHOLD_C,
    PROB_ICE_ZERO_THRESHOLD_C,
    PTYPE_DOMINANT_FRACTION,
    PTYPE_MIX_FRACTION_MIN,
    PTYPE_RATE_MIN_KG_M2_S,
    PTYPE_WET_SNOW_TW_MAX_C,
    PTYPE_WET_SNOW_TW_MIN_C,
    SHALLOW_SURFACE_REFREEZING_JKG,
    SMALL_AREA_THRESHOLD_JKG,
    SNOW_MELTING_ENERGY_DECAY,
    SNOW_PROBABILITY_PREFACTOR,
    SUBLIMATION_MIN_DEPTH_M,
)

njit = _numba.njit


@njit(cache=True)
def _clip(value: float) -> float:
    if not np.isfinite(value):
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value


@njit(cache=True)
def _prob_ice_from_temperature(temperature_c: float) -> float:
    if temperature_c <= PROB_ICE_FULL_THRESHOLD_C:
        return 100.0
    if temperature_c >= PROB_ICE_ZERO_THRESHOLD_C:
        return 0.0
    return _clip(
        -0.065 * temperature_c**4
        - 3.1544 * temperature_c**3
        - 56.414 * temperature_c**2
        - 449.6 * temperature_c
        - 1308.0
    )


@njit(cache=True)
def _profile_start(full_level_height_m: np.ndarray, vertical_cutoff_m: float) -> int:
    for index in range(full_level_height_m.size):
        if full_level_height_m[index] <= vertical_cutoff_m:
            return index
    return 0


@njit(cache=True)
def _build_layers(
    relative_humidity_ice_pct: np.ndarray,
    temperature_c: np.ndarray,
    half_level_height_m: np.ndarray,
    start: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    nlev = temperature_c.size
    is_precipitating = np.empty(nlev, dtype=np.int8)
    starts = np.empty(nlev, dtype=np.int64)
    depths = np.empty(nlev, dtype=np.float64)
    minimum_temperatures = np.empty(nlev, dtype=np.float64)
    count = 0
    current_start = start
    current_is_precipitating = 1 if relative_humidity_ice_pct[start] > PRECIP_GENERATION_RHI_THRESHOLD_PCT else 0
    current_minimum_temperature = temperature_c[start]
    for level in range(start + 1, nlev):
        next_is_precipitating = 1 if relative_humidity_ice_pct[level] > PRECIP_GENERATION_RHI_THRESHOLD_PCT else 0
        if next_is_precipitating != current_is_precipitating:
            is_precipitating[count] = current_is_precipitating
            starts[count] = current_start
            depths[count] = half_level_height_m[current_start] - half_level_height_m[level]
            minimum_temperatures[count] = current_minimum_temperature
            count += 1
            current_start = level
            current_is_precipitating = next_is_precipitating
            current_minimum_temperature = temperature_c[level]
        elif temperature_c[level] < current_minimum_temperature:
            current_minimum_temperature = temperature_c[level]
    is_precipitating[count] = current_is_precipitating
    starts[count] = current_start
    depths[count] = half_level_height_m[current_start] - half_level_height_m[nlev]
    minimum_temperatures[count] = current_minimum_temperature
    return is_precipitating, starts, depths, minimum_temperatures, count + 1


@njit(cache=True)
def _prob_ice(
    is_precipitating: np.ndarray,
    starts: np.ndarray,
    depths: np.ndarray,
    minimum_temperatures: np.ndarray,
    layer_count: int,
    relaxed: bool,
) -> tuple[float, bool]:
    max_sublimation_start = -1
    for index in range(layer_count):
        if is_precipitating[index] == 0 and depths[index] > SUBLIMATION_MIN_DEPTH_M:
            if starts[index] > max_sublimation_start:
                max_sublimation_start = starts[index]

    has_precip_layer = False
    has_valid_layer = False
    lowest_precip_start = -1
    lowest_temperature = 0.0
    best_valid_temperature = 0.0
    for index in range(layer_count):
        if is_precipitating[index] == 0:
            continue
        if (not relaxed) and depths[index] <= PRECIP_GENERATION_MIN_DEPTH_M:
            continue
        has_precip_layer = True
        if starts[index] > lowest_precip_start:
            lowest_precip_start = starts[index]
            lowest_temperature = minimum_temperatures[index]
        if starts[index] >= max_sublimation_start:
            if (not has_valid_layer) or minimum_temperatures[index] < best_valid_temperature:
                best_valid_temperature = minimum_temperatures[index]
                has_valid_layer = True

    if not has_precip_layer:
        if relaxed and layer_count > 0:
            minimum_temperature = minimum_temperatures[0]
            for index in range(1, layer_count):
                if minimum_temperatures[index] < minimum_temperature:
                    minimum_temperature = minimum_temperatures[index]
            return _prob_ice_from_temperature(minimum_temperature), True
        return 0.0, False
    if has_valid_layer:
        return _prob_ice_from_temperature(best_valid_temperature), True
    if relaxed:
        return _prob_ice_from_temperature(lowest_temperature), True
    return 0.0, False


@njit(cache=True)
def _prepare_signs(wet_bulb_c: np.ndarray, start: int) -> np.ndarray:
    signs = np.zeros(wet_bulb_c.size, dtype=np.int8)
    for level in range(start, wet_bulb_c.size):
        if wet_bulb_c[level] > 0.0:
            signs[level] = 1
        elif wet_bulb_c[level] < 0.0:
            signs[level] = -1
    for level in range(start, wet_bulb_c.size):
        if signs[level] != 0:
            continue
        if level > start:
            signs[level] = signs[level - 1]
        else:
            signs[level] = 1
            for next_level in range(start + 1, wet_bulb_c.size):
                if signs[next_level] != 0:
                    signs[level] = signs[next_level]
                    break
    return signs


@njit(cache=True)
def _append_energy(segment_energy: float, merged: np.ndarray, count: int) -> int:
    if abs(segment_energy) < SMALL_AREA_THRESHOLD_JKG:
        return count
    if count > 0 and ((merged[count - 1] > 0.0 and segment_energy > 0.0) or (merged[count - 1] < 0.0 and segment_energy < 0.0)):
        merged[count - 1] += segment_energy
        return count
    merged[count] = segment_energy
    return count + 1


@njit(cache=True)
def _energies_surface_up(
    wet_bulb_c: np.ndarray,
    full_level_height_m: np.ndarray,
    start: int,
) -> tuple[np.ndarray, int, float]:
    signs = _prepare_signs(wet_bulb_c, start)
    merged = np.empty(wet_bulb_c.size, dtype=np.float64)
    merged_count = 0
    current_sign = signs[start]
    segment_energy = 0.0
    keep_segment = False
    column_melting_energy = 0.0
    for level in range(start, wet_bulb_c.size - 1):
        delta_z = full_level_height_m[level] - full_level_height_m[level + 1]
        if signs[level + 1] == current_sign:
            layer_energy = 0.5 * (abs(wet_bulb_c[level]) + abs(wet_bulb_c[level + 1])) / KELVIN_OFFSET * delta_z * GRAVITY
            segment_energy += current_sign * layer_energy
            if current_sign > 0:
                column_melting_energy += layer_energy
            continue
        denominator = abs(wet_bulb_c[level]) + abs(wet_bulb_c[level + 1])
        upper_fraction = abs(wet_bulb_c[level]) / denominator
        upper_energy = 0.5 * abs(wet_bulb_c[level]) / KELVIN_OFFSET * upper_fraction * delta_z * GRAVITY
        lower_energy = 0.5 * abs(wet_bulb_c[level + 1]) / KELVIN_OFFSET * (1.0 - upper_fraction) * delta_z * GRAVITY
        if current_sign > 0:
            column_melting_energy += upper_energy
        if signs[level + 1] > 0:
            column_melting_energy += lower_energy
        segment_energy += current_sign * upper_energy
        if keep_segment:
            merged_count = _append_energy(segment_energy, merged, merged_count)
        segment_energy = signs[level + 1] * lower_energy
        current_sign = signs[level + 1]
        keep_segment = True
    if keep_segment:
        merged_count = _append_energy(segment_energy, merged, merged_count)

    energies = np.zeros(3, dtype=np.float64)
    limit = 2 if wet_bulb_c[wet_bulb_c.size - 1] < 0.0 else 3
    if merged_count < limit:
        limit = merged_count
    for index in range(limit):
        energies[index] = merged[merged_count - 1 - index]
    return energies, limit, column_melting_energy


@njit(cache=True)
def _categorical(probabilities: tuple[float, float, float, float, float, float]) -> int:
    rain, snow, ice_pellets, freezing_drizzle, freezing_rain_on_ground, freezing_rain = probabilities
    highest = freezing_rain
    code = 3
    if freezing_rain_on_ground > highest:
        highest = freezing_rain_on_ground
        code = 13
    if ice_pellets > highest:
        highest = ice_pellets
        code = 8
    if freezing_drizzle > highest:
        highest = freezing_drizzle
        code = 12
    if snow > highest:
        highest = snow
        code = 5
    if rain > highest:
        highest = rain
        code = 1
    if highest <= 0.0:
        return 0
    return code


@njit(cache=True)
def _no_areas(prob_ice: float, surface_tw_c: float, ground_temperature_c: float) -> tuple[float, float, float, float, float, float]:
    snow = prob_ice
    freezing_drizzle = 100.0 - snow
    freezing_rain_on_ground = 0.0
    rain = 0.0
    if surface_tw_c > 0.0:
        rain = freezing_drizzle
        freezing_drizzle = 0.0
        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain
    return _clip(rain), _clip(snow), 0.0, _clip(freezing_drizzle), _clip(freezing_rain_on_ground), 0.0


@njit(cache=True)
def _snow_probability(prob_ice: float, column_melting_energy: float) -> float:
    snow_ice = _clip(SNOW_PROBABILITY_PREFACTOR * np.exp(-SNOW_MELTING_ENERGY_DECAY * column_melting_energy))
    return _clip(prob_ice / 100.0 * snow_ice)


@njit(cache=True)
def _ice_pellet_probability(refreezing_energy: float, melting_energy: float) -> float:
    if melting_energy <= -1.0:
        return 0.0
    return _clip(2.3 * refreezing_energy - 42.0 * np.log(melting_energy + 1.0) + 3.0)


@njit(cache=True)
def _rain_or_freezing_rain(prob_ice: float, refreezing_energy: float, melting_energy: float) -> float:
    ice_probability = _clip(
        FREEZING_RAIN_REFREEZE_COEFF * refreezing_energy
        + FREEZING_RAIN_MELT_COEFF * melting_energy
        + FREEZING_RAIN_INTERCEPT
    )
    if melting_energy < FREEZING_RAIN_LOW_MELT_THRESHOLD_JKG:
        ice_probability *= FREEZING_RAIN_MELT_COEFF * melting_energy
    return _clip((100.0 - prob_ice) + prob_ice / 100.0 * ice_probability)


@njit(cache=True)
def _areas_to_probabilities(
    energies: np.ndarray,
    energy_count: int,
    column_melting_energy: float,
    prob_ice: float,
    surface_tw_c: float,
    ground_temperature_c: float,
) -> tuple[float, float, float, float, float, float]:
    if energy_count == 0:
        return _no_areas(prob_ice, surface_tw_c, ground_temperature_c)
    freezing_rain = 0.0
    freezing_rain_on_ground = 0.0
    ice_pellets = 0.0
    snow = 0.0
    rain = 0.0
    if surface_tw_c < 0.0:
        refreezing_energy = 0.0
        melting_energy = 0.0
        if energies[0] < 0.0:
            refreezing_energy = abs(energies[0])
            if energy_count >= 2 and energies[1] > 0.0:
                melting_energy = energies[1]
        elif energies[0] > 0.0:
            melting_energy = energies[0]
            refreezing_energy = SHALLOW_SURFACE_REFREEZING_JKG
        if melting_energy <= 0.0:
            return _no_areas(prob_ice, surface_tw_c, ground_temperature_c)
        snow = _snow_probability(prob_ice, column_melting_energy)
        ice_pellets = _clip(prob_ice / 100.0 * _ice_pellet_probability(refreezing_energy, melting_energy))
        if refreezing_energy > LARGE_REFREEZING_SNOW_THRESHOLD_JKG:
            if ice_pellets > snow:
                snow = ice_pellets
            ice_pellets = 0.0
        freezing_rain = _rain_or_freezing_rain(prob_ice, refreezing_energy, melting_energy)
    else:
        surface_melting_energy = energies[0] if energies[0] > 0.0 else 0.0
        refreezing_energy = abs(energies[1]) if energy_count >= 2 and energies[1] < 0.0 else 0.0
        upper_melting_energy = energies[2] if energy_count >= 3 and energies[2] > 0.0 else 0.0
        if surface_melting_energy <= 0.0:
            return _no_areas(prob_ice, surface_tw_c, ground_temperature_c)
        if refreezing_energy > 0.0 and upper_melting_energy > 0.0:
            ice_pellets = _clip(prob_ice / 100.0 * _ice_pellet_probability(refreezing_energy, upper_melting_energy))
            melted_ice_pellets = 0.0
            if surface_melting_energy >= ICE_PELLET_SURFACE_MELT_RAIN_MIN_JKG:
                melted_ice_pellets = ice_pellets
                ice_pellets = 0.0
            elif surface_melting_energy > ICE_PELLET_SURFACE_MELT_KEEP_MAX_JKG:
                keep_factor = (ICE_PELLET_SURFACE_MELT_RAIN_MIN_JKG - surface_melting_energy) / (
                    ICE_PELLET_SURFACE_MELT_RAIN_MIN_JKG - ICE_PELLET_SURFACE_MELT_KEEP_MAX_JKG
                )
                melted_ice_pellets = ice_pellets * (1.0 - keep_factor)
                ice_pellets *= keep_factor
            rain = _rain_or_freezing_rain(prob_ice, refreezing_energy, upper_melting_energy)
            if melted_ice_pellets > rain:
                rain = melted_ice_pellets
            snow = _snow_probability(prob_ice, column_melting_energy)
        else:
            snow = _snow_probability(prob_ice, column_melting_energy)
            rain = 100.0 - snow
        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain
    return _clip(rain), _clip(snow), _clip(ice_pellets), 0.0, _clip(freezing_rain_on_ground), _clip(freezing_rain)


@njit(cache=True)
def _refine(code: int, surface_tw_c: float, rain: float, snow: float, graupel: float, hail: float) -> int:
    if code == 0:
        return code
    rain = max(0.0, rain)
    snow = max(0.0, snow)
    graupel = max(0.0, graupel)
    hail = max(0.0, hail)
    total = rain + snow + graupel + hail
    if total <= PTYPE_RATE_MIN_KG_M2_S:
        return code
    rain_fraction = rain / total
    snow_fraction = snow / total
    graupel_fraction = graupel / total
    hail_fraction = hail / total
    if hail >= PTYPE_RATE_MIN_KG_M2_S and hail_fraction >= PTYPE_DOMINANT_FRACTION:
        return 10
    if graupel >= PTYPE_RATE_MIN_KG_M2_S and graupel_fraction >= PTYPE_DOMINANT_FRACTION:
        return 9
    if code == 3 or code == 12 or code == 13 or code == 8:
        return code
    rain_material = rain >= PTYPE_RATE_MIN_KG_M2_S and rain_fraction >= PTYPE_MIX_FRACTION_MIN
    snow_material = snow >= PTYPE_RATE_MIN_KG_M2_S and snow_fraction >= PTYPE_MIX_FRACTION_MIN
    if rain_material and snow_material:
        return 7
    if (
        code == 5
        and snow >= PTYPE_RATE_MIN_KG_M2_S
        and not rain_material
        and surface_tw_c >= PTYPE_WET_SNOW_TW_MIN_C
        and surface_tw_c <= PTYPE_WET_SNOW_TW_MAX_C
    ):
        return 6
    return code


@njit(cache=True)
def diagnose_icon_column_probabilities_numba(
    temperature_c: np.ndarray,
    wet_bulb_c: np.ndarray,
    relative_humidity_ice_pct: np.ndarray,
    full_level_height_m: np.ndarray,
    half_level_height_m: np.ndarray,
    total_precip_mm: float,
    ground_temperature_c: float,
    rain_rate: float,
    snow_rate: float,
    graupel_rate: float,
    hail_rate: float,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float,
) -> tuple[int, float, float, float, float, float, float]:
    if total_precip_mm <= precip_mask_threshold_mm:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    start = _profile_start(full_level_height_m, vertical_cutoff_m)
    is_precipitating, starts, depths, minimum_temperatures, layer_count = _build_layers(
        relative_humidity_ice_pct, temperature_c, half_level_height_m, start
    )
    prob_ice, has_generation = _prob_ice(is_precipitating, starts, depths, minimum_temperatures, layer_count, False)
    energies, energy_count, column_melting_energy = _energies_surface_up(wet_bulb_c, full_level_height_m, start)
    probabilities = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    if has_generation:
        probabilities = _areas_to_probabilities(
            energies,
            energy_count,
            column_melting_energy,
            prob_ice,
            wet_bulb_c[wet_bulb_c.size - 1],
            ground_temperature_c,
        )
    core_code = _categorical(probabilities)
    if core_code == 0:
        prob_ice, has_generation = _prob_ice(is_precipitating, starts, depths, minimum_temperatures, layer_count, True)
        if has_generation:
            probabilities = _areas_to_probabilities(
                energies,
                energy_count,
                column_melting_energy,
                prob_ice,
                wet_bulb_c[wet_bulb_c.size - 1],
                ground_temperature_c,
            )
            core_code = _categorical(probabilities)
    code = _refine(core_code, wet_bulb_c[wet_bulb_c.size - 1], rain_rate, snow_rate, graupel_rate, hail_rate)
    return code, probabilities[0], probabilities[1], probabilities[2], probabilities[3], probabilities[4], probabilities[5]


@njit(cache=True)
def diagnose_icon_grid_probabilities_numba_kernel(
    temperature_c_2d: np.ndarray,
    wet_bulb_c_2d: np.ndarray,
    relative_humidity_ice_pct_2d: np.ndarray,
    full_level_height_m_2d: np.ndarray,
    half_level_height_m_2d: np.ndarray,
    total_precip_mm: np.ndarray,
    ground_temperature_c: np.ndarray,
    rain_rate: np.ndarray,
    snow_rate: np.ndarray,
    graupel_rate: np.ndarray,
    hail_rate: np.ndarray,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    categorical = np.zeros(total_precip_mm.size, dtype=np.int32)
    probabilities = np.zeros((6, total_precip_mm.size), dtype=np.float64)
    for index in range(total_precip_mm.size):
        result = diagnose_icon_column_probabilities_numba(
            temperature_c_2d[:, index],
            wet_bulb_c_2d[:, index],
            relative_humidity_ice_pct_2d[:, index],
            full_level_height_m_2d[:, index],
            half_level_height_m_2d[:, index],
            total_precip_mm[index],
            ground_temperature_c[index],
            rain_rate[index],
            snow_rate[index],
            graupel_rate[index],
            hail_rate[index],
            precip_mask_threshold_mm,
            vertical_cutoff_m,
        )
        categorical[index] = result[0]
        probabilities[0, index] = result[1]
        probabilities[1, index] = result[2]
        probabilities[2, index] = result[3]
        probabilities[3, index] = result[4]
        probabilities[4, index] = result[5]
        probabilities[5, index] = result[6]
    return categorical, probabilities


@njit(cache=True)
def diagnose_icon_grid_categorical_numba_kernel(
    temperature_c_2d: np.ndarray,
    wet_bulb_c_2d: np.ndarray,
    relative_humidity_ice_pct_2d: np.ndarray,
    full_level_height_m_2d: np.ndarray,
    half_level_height_m_2d: np.ndarray,
    total_precip_mm: np.ndarray,
    ground_temperature_c: np.ndarray,
    rain_rate: np.ndarray,
    snow_rate: np.ndarray,
    graupel_rate: np.ndarray,
    hail_rate: np.ndarray,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float,
) -> np.ndarray:
    categorical, _ = diagnose_icon_grid_probabilities_numba_kernel(
        temperature_c_2d,
        wet_bulb_c_2d,
        relative_humidity_ice_pct_2d,
        full_level_height_m_2d,
        half_level_height_m_2d,
        total_precip_mm,
        ground_temperature_c,
        rain_rate,
        snow_rate,
        graupel_rate,
        hail_rate,
        precip_mask_threshold_mm,
        vertical_cutoff_m,
    )
    return categorical
