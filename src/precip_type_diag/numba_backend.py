"""Numba-accelerated categorical precipitation-type kernel."""

from __future__ import annotations

import numpy as np

import numba as _numba

from .constants import (
    GRAVITY,
    GROUND_FREEZING_THRESHOLD_C,
    KELVIN_OFFSET,
    PRECIP_GENERATION_MIN_DEPTH_M,
    PRECIP_GENERATION_RHI_THRESHOLD_PCT,
    PROB_ICE_FULL_THRESHOLD_C,
    PROB_ICE_ZERO_THRESHOLD_C,
    SHALLOW_SURFACE_REFREEZING_JKG,
    SMALL_AREA_THRESHOLD_JKG,
    SUBLIMATION_MIN_DEPTH_M,
)

njit = _numba.njit


@njit(cache=True)
def _clip_probability_numba(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return value


@njit(cache=True)
def _prob_ice_from_temperature_numba(min_temperature_c: float) -> float:
    if min_temperature_c <= PROB_ICE_FULL_THRESHOLD_C:
        return 100.0
    if min_temperature_c >= PROB_ICE_ZERO_THRESHOLD_C:
        return 0.0
    return _clip_probability_numba(
        -0.065 * min_temperature_c**4
        - 3.1544 * min_temperature_c**3
        - 56.414 * min_temperature_c**2
        - 449.6 * min_temperature_c
        - 1308.0
    )


@njit(cache=True)
def _build_layers_numba(
    relative_humidity_ice_pct: np.ndarray,
    temperature_c: np.ndarray,
    full_level_height_m: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]:
    n_levels = relative_humidity_ice_pct.size
    is_precipitating = np.empty(n_levels, dtype=np.int8)
    starts = np.empty(n_levels, dtype=np.int64)
    depths = np.empty(n_levels, dtype=np.float64)
    minimum_temperatures = np.empty(n_levels, dtype=np.float64)

    if n_levels == 0:
        return is_precipitating, starts, depths, minimum_temperatures, 0

    count = 0
    current_value = 1 if relative_humidity_ice_pct[0] > PRECIP_GENERATION_RHI_THRESHOLD_PCT else 0
    current_start = 0
    current_min_height = full_level_height_m[0]
    current_max_height = full_level_height_m[0]
    current_min_temperature = temperature_c[0]

    for index in range(1, n_levels):
        next_value = 1 if relative_humidity_ice_pct[index] > PRECIP_GENERATION_RHI_THRESHOLD_PCT else 0
        if next_value != current_value:
            is_precipitating[count] = current_value
            starts[count] = current_start
            depths[count] = current_max_height - current_min_height
            minimum_temperatures[count] = current_min_temperature
            count += 1

            current_value = next_value
            current_start = index
            current_min_height = full_level_height_m[index]
            current_max_height = full_level_height_m[index]
            current_min_temperature = temperature_c[index]
            continue

        if full_level_height_m[index] < current_min_height:
            current_min_height = full_level_height_m[index]
        if full_level_height_m[index] > current_max_height:
            current_max_height = full_level_height_m[index]
        if temperature_c[index] < current_min_temperature:
            current_min_temperature = temperature_c[index]

    is_precipitating[count] = current_value
    starts[count] = current_start
    depths[count] = current_max_height - current_min_height
    minimum_temperatures[count] = current_min_temperature
    count += 1

    return is_precipitating, starts, depths, minimum_temperatures, count


@njit(cache=True)
def _prob_ice_numba(
    is_precipitating: np.ndarray,
    starts: np.ndarray,
    depths: np.ndarray,
    minimum_temperatures: np.ndarray,
    layer_count: int,
    relaxed_precip_layers: bool,
) -> tuple[float, bool]:
    max_sublimation_start = -1
    has_precip_layer = False
    has_valid_layer = False
    best_valid_min_temperature = 0.0
    lowest_precip_start = -1
    lowest_precip_temperature = 0.0

    for index in range(layer_count):
        if is_precipitating[index] == 0 and depths[index] > SUBLIMATION_MIN_DEPTH_M:
            if starts[index] > max_sublimation_start:
                max_sublimation_start = starts[index]

    for index in range(layer_count):
        if is_precipitating[index] == 0:
            continue
        if (not relaxed_precip_layers) and depths[index] <= PRECIP_GENERATION_MIN_DEPTH_M:
            continue

        has_precip_layer = True
        if starts[index] > lowest_precip_start:
            lowest_precip_start = starts[index]
            lowest_precip_temperature = minimum_temperatures[index]

        if starts[index] >= max_sublimation_start:
            if (not has_valid_layer) or minimum_temperatures[index] < best_valid_min_temperature:
                best_valid_min_temperature = minimum_temperatures[index]
                has_valid_layer = True

    if not has_precip_layer:
        return 0.0, False
    if has_valid_layer:
        return _prob_ice_from_temperature_numba(best_valid_min_temperature), True
    if relaxed_precip_layers:
        return _prob_ice_from_temperature_numba(lowest_precip_temperature), True
    return 0.0, False


@njit(cache=True)
def _prepare_signs_numba(wet_bulb_c: np.ndarray) -> np.ndarray:
    signs = np.empty(wet_bulb_c.size, dtype=np.int8)
    all_zero = True
    for index in range(wet_bulb_c.size):
        if wet_bulb_c[index] > 0.0:
            signs[index] = 1
            all_zero = False
        elif wet_bulb_c[index] < 0.0:
            signs[index] = -1
            all_zero = False
        else:
            signs[index] = 0

    if all_zero:
        for index in range(signs.size):
            signs[index] = 1
        return signs

    for index in range(signs.size):
        if signs[index] != 0:
            continue
        prev_index = index - 1
        next_index = index + 1
        while prev_index >= 0 and signs[prev_index] == 0:
            prev_index -= 1
        while next_index < signs.size and signs[next_index] == 0:
            next_index += 1
        if prev_index >= 0 and signs[prev_index] != 0:
            signs[index] = signs[prev_index]
        elif next_index < signs.size and signs[next_index] != 0:
            signs[index] = signs[next_index]
        else:
            signs[index] = 1
    return signs


@njit(cache=True)
def _segment_energy_numba(
    wet_bulb_c: np.ndarray,
    full_level_height_m: np.ndarray,
    start: int,
    end: int,
) -> float:
    if end - start < 2:
        return 0.0

    total = 0.0
    for index in range(start, end - 1):
        mean_tw_c = 0.5 * (wet_bulb_c[index] + wet_bulb_c[index + 1])
        dz = full_level_height_m[index] - full_level_height_m[index + 1]
        contribution = (mean_tw_c / KELVIN_OFFSET) * dz * GRAVITY
        if not np.isnan(contribution):
            total += contribution
    return total


@njit(cache=True)
def _energies_surface_up_numba(
    wet_bulb_c: np.ndarray,
    full_level_height_m: np.ndarray,
) -> tuple[np.ndarray, int]:
    n_levels = wet_bulb_c.size
    merged_energies = np.empty(n_levels, dtype=np.float64)
    merged_count = 0

    signs = _prepare_signs_numba(wet_bulb_c)
    current_sign = signs[0]
    current_start = 0
    segment_count = 0

    for index in range(1, n_levels + 1):
        segment_ends = index == n_levels or signs[index] != current_sign
        if not segment_ends:
            continue

        if segment_count > 0:
            energy = _segment_energy_numba(wet_bulb_c, full_level_height_m, current_start, index)
            if abs(energy) >= SMALL_AREA_THRESHOLD_JKG:
                if merged_count > 0:
                    previous_energy = merged_energies[merged_count - 1]
                    if (previous_energy > 0.0 and energy > 0.0) or (previous_energy < 0.0 and energy < 0.0):
                        merged_energies[merged_count - 1] = previous_energy + energy
                    else:
                        merged_energies[merged_count] = energy
                        merged_count += 1
                else:
                    merged_energies[merged_count] = energy
                    merged_count += 1

        segment_count += 1
        if index < n_levels:
            current_sign = signs[index]
            current_start = index

    energies = np.zeros(3, dtype=np.float64)
    energy_count = 0
    energy_limit = 2 if wet_bulb_c[wet_bulb_c.size - 1] < 0.0 else 3
    if merged_count < energy_limit:
        energy_limit = merged_count

    for index in range(energy_limit):
        energies[index] = merged_energies[merged_count - 1 - index]
        energy_count += 1

    return energies, energy_count


@njit(cache=True)
def _categorical_code_numba(
    freezing_rain: float,
    freezing_rain_on_ground: float,
    ice_pellets: float,
    freezing_drizzle: float,
    snow: float,
    rain: float,
) -> int:
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
def _no_areas_code_numba(prob_ice: float, surface_tw_c: float) -> int:
    snow = _clip_probability_numba(prob_ice)
    freezing_drizzle = _clip_probability_numba(100.0 - snow)
    rain = 0.0
    if surface_tw_c > 0.0:
        rain = freezing_drizzle
        freezing_drizzle = 0.0
    return _categorical_code_numba(0.0, 0.0, 0.0, freezing_drizzle, snow, rain)


@njit(cache=True)
def _areas_to_code_numba(
    energies: np.ndarray,
    energy_count: int,
    prob_ice: float,
    surface_tw_c: float,
    ground_temperature_c: float,
) -> int:
    if energy_count == 0:
        return _no_areas_code_numba(prob_ice, surface_tw_c)

    freezing_rain = 0.0
    freezing_rain_on_ground = 0.0
    ice_pellets = 0.0
    freezing_drizzle = 0.0
    snow = 0.0
    rain = 0.0

    if energy_count == 1:
        melting_energy = energies[0]
        snow_ice = _clip_probability_numba(1540.0 * np.exp(-0.29 * melting_energy))
        snow = _clip_probability_numba((prob_ice / 100.0) * snow_ice)
        rain = 100.0 - snow
        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain
        if surface_tw_c < 0.0:
            refreezing_energy = SHALLOW_SURFACE_REFREEZING_JKG
            fzra_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
            if melting_energy < 5.0:
                fzra_ice *= 0.2 * melting_energy
            freezing_rain = _clip_probability_numba(
                (100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability_numba(fzra_ice)
            )

            ice_pellet_ice = _clip_probability_numba(2.3 * refreezing_energy - 42.0 * np.log(melting_energy + 1.0) + 3.0)
            ice_pellets = _clip_probability_numba((prob_ice / 100.0) * ice_pellet_ice)
            rain = 0.0
            freezing_rain_on_ground = 0.0

    elif energy_count >= 2 and energy_count % 2 == 0:
        melting_energy = energies[1]
        refreezing_energy = abs(energies[0])

        snow_ice = _clip_probability_numba(1540.0 * np.exp(-0.28 * melting_energy))
        snow = _clip_probability_numba((prob_ice / 100.0) * snow_ice)

        ice_pellet_ice = _clip_probability_numba(2.3 * refreezing_energy - 42.0 * np.log(melting_energy + 1.0) + 3.0)
        ice_pellets = _clip_probability_numba((prob_ice / 100.0) * ice_pellet_ice)

        fzra_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
        if melting_energy < 5.0:
            fzra_ice *= 0.2 * melting_energy
        freezing_rain = _clip_probability_numba(
            (100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability_numba(fzra_ice)
        )

        if surface_tw_c > 0.0:
            freezing_rain = 0.0

    else:
        upper_melting_energy = energies[2]
        surface_melting_energy = energies[0]
        total_melting_energy = upper_melting_energy + surface_melting_energy
        refreezing_energy = abs(energies[1])

        ice_pellet_ice = _clip_probability_numba(2.3 * refreezing_energy - 42.0 * np.log(upper_melting_energy + 1.0) + 3.0)
        ice_pellets = _clip_probability_numba((prob_ice / 100.0) * ice_pellet_ice)

        rain_ice = -2.1 * refreezing_energy + 0.2 * upper_melting_energy + 458.0
        if upper_melting_energy < 5.0:
            rain_ice *= 0.2 * upper_melting_energy
        rain = _clip_probability_numba((100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability_numba(rain_ice))

        snow_ice = _clip_probability_numba(1540.0 * np.exp(-0.28 * total_melting_energy))
        snow = _clip_probability_numba((prob_ice / 100.0) * snow_ice)

        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain

        if surface_tw_c < 0.0:
            refreezing_energy = SHALLOW_SURFACE_REFREEZING_JKG
            melting_energy = energies[0]
            fzra_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
            if melting_energy < 5.0:
                fzra_ice *= 0.2 * melting_energy
            freezing_rain = _clip_probability_numba(
                (100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability_numba(fzra_ice)
            )
            rain = 0.0
            freezing_rain_on_ground = 0.0

    return _categorical_code_numba(
        _clip_probability_numba(freezing_rain),
        _clip_probability_numba(freezing_rain_on_ground),
        _clip_probability_numba(ice_pellets),
        _clip_probability_numba(freezing_drizzle),
        _clip_probability_numba(snow),
        _clip_probability_numba(rain),
    )


@njit(cache=True)
def diagnose_column_categorical_numba(
    temperature_c: np.ndarray,
    wet_bulb_c: np.ndarray,
    relative_humidity_ice_pct: np.ndarray,
    full_level_height_m: np.ndarray,
    total_precip_mm: float,
    ground_temperature_c: float,
    precip_mask_threshold_mm: float,
) -> int:
    if total_precip_mm <= precip_mask_threshold_mm:
        return 0

    (
        is_precipitating,
        starts,
        depths,
        minimum_temperatures,
        layer_count,
    ) = _build_layers_numba(relative_humidity_ice_pct, temperature_c, full_level_height_m)

    pure_prob_ice, pure_has_precip_generation = _prob_ice_numba(
        is_precipitating,
        starts,
        depths,
        minimum_temperatures,
        layer_count,
        False,
    )
    energies, energy_count = _energies_surface_up_numba(wet_bulb_c, full_level_height_m)

    pure_code = 0
    if pure_has_precip_generation:
        pure_code = _areas_to_code_numba(
            energies,
            energy_count,
            pure_prob_ice,
            wet_bulb_c[wet_bulb_c.size - 1],
            ground_temperature_c,
        )
    if pure_code != 0:
        return pure_code

    microphysics_prob_ice, microphysics_has_precip_generation = _prob_ice_numba(
        is_precipitating,
        starts,
        depths,
        minimum_temperatures,
        layer_count,
        True,
    )
    if not microphysics_has_precip_generation:
        return 0

    return _areas_to_code_numba(
        energies,
        energy_count,
        microphysics_prob_ice,
        wet_bulb_c[wet_bulb_c.size - 1],
        ground_temperature_c,
    )


@njit(cache=True)
def diagnose_grid_categorical_numba_kernel(
    temperature_c_2d: np.ndarray,
    wet_bulb_c_2d: np.ndarray,
    relative_humidity_ice_pct_2d: np.ndarray,
    full_level_height_m_2d: np.ndarray,
    total_precip_mm: np.ndarray,
    ground_temperature_c: np.ndarray,
    precip_mask_threshold_mm: float,
) -> np.ndarray:
    output = np.zeros(total_precip_mm.size, dtype=np.int32)
    for index in range(total_precip_mm.size):
        output[index] = diagnose_column_categorical_numba(
            temperature_c_2d[:, index],
            wet_bulb_c_2d[:, index],
            relative_humidity_ice_pct_2d[:, index],
            full_level_height_m_2d[:, index],
            total_precip_mm[index],
            ground_temperature_c[index],
            precip_mask_threshold_mm,
        )
    return output
