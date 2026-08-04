"""Scalar ICON-like diagnostic aligned with ICON commit 50da7c5924994f7626688eb5185b8e66c781b12e."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .constants import DEFAULT_VERTICAL_CUTOFF_M, PrecipitationTypeCode
from .profile import ThermodynamicColumn, TypeProbabilities

KELVIN_OFFSET = 273.15
GRAVITY = 9.80665
RD = 287.04
RV = 461.51
RDV = RD / RV

# MeteoSwiss ICON uses itype_satpres_coeffs=2 (IFS coefficients).
SATURATION_B1 = 610.78
SATURATION_WATER_B2 = 17.502
SATURATION_WATER_B4 = 32.19
SATURATION_ICE_B2 = 22.587
SATURATION_ICE_B4 = -0.7

PRECIP_GENERATION_RHI_THRESHOLD_PCT = 75.0
PRECIP_GENERATION_MIN_DEPTH_M = 1000.0
SUBLIMATION_MIN_DEPTH_M = 1500.0
PROB_ICE_FULL_THRESHOLD_C = -15.0
PROB_ICE_ZERO_THRESHOLD_C = -7.0
SMALL_AREA_THRESHOLD_JKG = 2.0
SHALLOW_SURFACE_REFREEZING_JKG = 1.0
GROUND_FREEZING_THRESHOLD_C = -3.0
ICE_PELLET_SURFACE_MELT_KEEP_MAX_JKG = 5.6
ICE_PELLET_SURFACE_MELT_RAIN_MIN_JKG = 13.2
LARGE_REFREEZING_SNOW_THRESHOLD_JKG = 600.0

SNOW_PROBABILITY_PREFACTOR = 1540.0
SNOW_MELTING_ENERGY_DECAY = 0.29
FREEZING_RAIN_REFREEZE_COEFF = -2.1
FREEZING_RAIN_MELT_COEFF = 0.2
FREEZING_RAIN_INTERCEPT = 458.0
FREEZING_RAIN_LOW_MELT_THRESHOLD_JKG = 5.0

PTYPE_RATE_MIN_KG_M2_S = 1.0e-6
PTYPE_DOMINANT_FRACTION = 0.70
PTYPE_MIX_FRACTION_MIN = 0.20
PTYPE_WET_SNOW_TW_MIN_C = -0.5
PTYPE_WET_SNOW_TW_MAX_C = 1.0
PTYPE_TRACE_RATE_THRESHOLD_MM_H = 0.01
PTYPE_TRACE_AMOUNT_FLOOR_MM = 1.0e-6

WETBULB_DELTA_WEIGHT = 0.5
WETBULB_OFFSET = 0.6
WETBULB_TEMP_SCALE = 700.0
WETBULB_OUTPUT_SCALE = 0.1
WETBULB_EXP_SCALE = 6400.0
WETBULB_EXP_SLOPE = 11.564
WETBULB_EXP_OFFSET = 1742.0


def _as_1d(array: np.ndarray | Iterable[float], name: str) -> np.ndarray:
    values = np.asarray(array, dtype=float)
    if values.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {values.shape}")
    return values


def _clip_probability(value: float) -> float:
    if not np.isfinite(value):
        return 0.0
    return float(min(100.0, max(0.0, value)))


def icon_precip_amount_threshold(interval_seconds: float) -> float:
    """Return the ICON interval-scaled trace precipitation amount threshold."""

    return max(PTYPE_TRACE_AMOUNT_FLOOR_MM, PTYPE_TRACE_RATE_THRESHOLD_MM_H * max(0.0, interval_seconds) / 3600.0)


@dataclass(frozen=True)
class SurfacePrecipitationRates:
    """Surface hydrometeor rates consumed by ICON's categorical refinement."""

    rain_kg_m2_s: float = 0.0
    snow_kg_m2_s: float = 0.0
    graupel_kg_m2_s: float = 0.0
    hail_kg_m2_s: float = 0.0


ZERO_SURFACE_PRECIPITATION_RATES = SurfacePrecipitationRates()


@dataclass(frozen=True)
class IconColumnProfile:
    """Raw model-input column for the ICON-like diagnostic."""

    temperature_k: np.ndarray
    pressure_pa: np.ndarray
    specific_humidity: np.ndarray
    full_level_height_m: np.ndarray
    half_level_height_m: np.ndarray
    total_precip_mm: float
    ground_temperature_c: float
    surface_rates: SurfacePrecipitationRates = ZERO_SURFACE_PRECIPITATION_RATES


@dataclass(frozen=True)
class IconColumnDiagnostics:
    """ICON-like core probabilities and thermodynamic/microphysics categories."""

    strict_prob_ice: float
    prob_ice: float
    strict_probabilities: TypeProbabilities
    probabilities: TypeProbabilities
    core_categorical_code: PrecipitationTypeCode
    categorical_code: PrecipitationTypeCode
    surface_wet_bulb_c: float


@dataclass(frozen=True)
class _Layer:
    is_precipitating: bool
    start: int
    depth_m: float
    minimum_temperature_c: float


def calculate_icon_thermodynamics(
    temperature_k: np.ndarray,
    specific_humidity: np.ndarray,
    pressure_pa: np.ndarray,
) -> ThermodynamicColumn:
    """Apply the ICON-native vapor-pressure/RH-ice and Egger-Joss formulas."""

    temperature_k = np.asarray(temperature_k, dtype=float)
    specific_humidity = np.maximum(np.asarray(specific_humidity, dtype=float), 1.0e-12)
    pressure_pa = np.asarray(pressure_pa, dtype=float)
    if temperature_k.shape != specific_humidity.shape or temperature_k.shape != pressure_pa.shape:
        raise ValueError("temperature_k, specific_humidity, and pressure_pa must have identical shapes")

    temperature_c = temperature_k - KELVIN_OFFSET
    vapor_pressure = specific_humidity * pressure_pa / (RDV + (1.0 - RDV) * specific_humidity)
    logfactor = np.log(vapor_pressure / SATURATION_B1) / SATURATION_WATER_B2
    dew_point_k = np.minimum(
        temperature_k,
        (KELVIN_OFFSET - SATURATION_WATER_B4 * logfactor) / (1.0 - logfactor),
    )
    saturation_ice = SATURATION_B1 * np.exp(
        SATURATION_ICE_B2 * (temperature_k - KELVIN_OFFSET) / (temperature_k - SATURATION_ICE_B4)
    )
    relative_humidity_ice_pct = 100.0 * vapor_pressure / saturation_ice

    temp_tenths_c = 10.0 * temperature_c
    dewpoint_tenths_c = 10.0 * (dew_point_k - KELVIN_OFFSET)
    delta_tenths_c = temp_tenths_c - dewpoint_tenths_c
    scaled_temperature = dewpoint_tenths_c + WETBULB_DELTA_WEIGHT * delta_tenths_c * (
        WETBULB_OFFSET - dewpoint_tenths_c / WETBULB_TEMP_SCALE
    )
    wet_bulb_c = WETBULB_OUTPUT_SCALE * (
        dewpoint_tenths_c
        + delta_tenths_c
        / (
            1.0
            + 10.0
            * WETBULB_EXP_SCALE
            * np.exp(WETBULB_EXP_SLOPE * scaled_temperature / (WETBULB_EXP_OFFSET + scaled_temperature))
            / pressure_pa
        )
    )
    return ThermodynamicColumn(
        temperature_c=temperature_c,
        wet_bulb_c=wet_bulb_c,
        relative_humidity_ice_pct=relative_humidity_ice_pct,
    )


def _prob_ice_from_temperature(min_temperature_c: float) -> float:
    if min_temperature_c <= PROB_ICE_FULL_THRESHOLD_C:
        return 100.0
    if min_temperature_c >= PROB_ICE_ZERO_THRESHOLD_C:
        return 0.0
    return _clip_probability(
        -0.065 * min_temperature_c**4
        - 3.1544 * min_temperature_c**3
        - 56.414 * min_temperature_c**2
        - 449.6 * min_temperature_c
        - 1308.0
    )


def _profile_start(full_level_height_m: np.ndarray, vertical_cutoff_m: float) -> int:
    indices = np.flatnonzero(full_level_height_m <= vertical_cutoff_m)
    return int(indices[0]) if indices.size else 0


def _build_layers(
    relative_humidity_ice_pct: np.ndarray,
    temperature_c: np.ndarray,
    half_level_height_m: np.ndarray,
    start: int,
) -> list[_Layer]:
    layers: list[_Layer] = []
    current_start = start
    current_is_precipitating = bool(relative_humidity_ice_pct[start] > PRECIP_GENERATION_RHI_THRESHOLD_PCT)
    current_minimum_temperature = float(temperature_c[start])
    for level in range(start + 1, temperature_c.size):
        next_is_precipitating = bool(relative_humidity_ice_pct[level] > PRECIP_GENERATION_RHI_THRESHOLD_PCT)
        if next_is_precipitating != current_is_precipitating:
            layers.append(
                _Layer(
                    is_precipitating=current_is_precipitating,
                    start=current_start,
                    depth_m=float(half_level_height_m[current_start] - half_level_height_m[level]),
                    minimum_temperature_c=current_minimum_temperature,
                )
            )
            current_start = level
            current_is_precipitating = next_is_precipitating
            current_minimum_temperature = float(temperature_c[level])
        else:
            current_minimum_temperature = min(current_minimum_temperature, float(temperature_c[level]))
    layers.append(
        _Layer(
            is_precipitating=current_is_precipitating,
            start=current_start,
            depth_m=float(half_level_height_m[current_start] - half_level_height_m[temperature_c.size]),
            minimum_temperature_c=current_minimum_temperature,
        )
    )
    return layers


def _prob_ice(layers: list[_Layer], *, relaxed: bool) -> tuple[float, bool]:
    max_sublimation_start = max(
        (layer.start for layer in layers if not layer.is_precipitating and layer.depth_m > SUBLIMATION_MIN_DEPTH_M),
        default=-1,
    )
    precip_layers = [
        layer
        for layer in layers
        if layer.is_precipitating and (relaxed or layer.depth_m > PRECIP_GENERATION_MIN_DEPTH_M)
    ]
    if not precip_layers:
        if relaxed and layers:
            return _prob_ice_from_temperature(min(layer.minimum_temperature_c for layer in layers)), True
        return 0.0, False
    valid_layers = [layer for layer in precip_layers if layer.start >= max_sublimation_start]
    if valid_layers:
        return _prob_ice_from_temperature(min(layer.minimum_temperature_c for layer in valid_layers)), True
    if relaxed:
        lowest = max(precip_layers, key=lambda layer: layer.start)
        return _prob_ice_from_temperature(lowest.minimum_temperature_c), True
    return 0.0, False


def _prepare_signs(wet_bulb_c: np.ndarray, start: int) -> np.ndarray:
    signs = np.zeros(wet_bulb_c.size, dtype=np.int8)
    signs[start:] = np.sign(wet_bulb_c[start:]).astype(np.int8)
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


def _append_energy(segment_energy: float, merged: list[float]) -> None:
    if abs(segment_energy) < SMALL_AREA_THRESHOLD_JKG:
        return
    if merged and np.sign(merged[-1]) == np.sign(segment_energy):
        merged[-1] += segment_energy
    else:
        merged.append(segment_energy)


def _energies_surface_up(
    wet_bulb_c: np.ndarray,
    full_level_height_m: np.ndarray,
    start: int,
) -> tuple[list[float], float]:
    signs = _prepare_signs(wet_bulb_c, start)
    current_sign = int(signs[start])
    segment_energy = 0.0
    keep_segment = False
    merged: list[float] = []
    column_melting_energy = 0.0
    for level in range(start, wet_bulb_c.size - 1):
        delta_z = float(full_level_height_m[level] - full_level_height_m[level + 1])
        if signs[level + 1] == current_sign:
            layer_energy = 0.5 * (abs(float(wet_bulb_c[level])) + abs(float(wet_bulb_c[level + 1]))) / KELVIN_OFFSET * delta_z * GRAVITY
            segment_energy += current_sign * layer_energy
            if current_sign > 0:
                column_melting_energy += layer_energy
            continue

        denominator = abs(float(wet_bulb_c[level])) + abs(float(wet_bulb_c[level + 1]))
        upper_fraction = abs(float(wet_bulb_c[level])) / denominator
        upper_energy = 0.5 * abs(float(wet_bulb_c[level])) / KELVIN_OFFSET * upper_fraction * delta_z * GRAVITY
        lower_energy = 0.5 * abs(float(wet_bulb_c[level + 1])) / KELVIN_OFFSET * (1.0 - upper_fraction) * delta_z * GRAVITY
        if current_sign > 0:
            column_melting_energy += upper_energy
        if signs[level + 1] > 0:
            column_melting_energy += lower_energy
        segment_energy += current_sign * upper_energy
        if keep_segment:
            _append_energy(segment_energy, merged)
        segment_energy = int(signs[level + 1]) * lower_energy
        current_sign = int(signs[level + 1])
        keep_segment = True
    if keep_segment:
        _append_energy(segment_energy, merged)
    limit = 2 if wet_bulb_c[-1] < 0.0 else 3
    return list(reversed(merged[-limit:])), column_melting_energy


def _ice_pellet_probability(refreezing_energy: float, melting_energy: float) -> float:
    if melting_energy <= -1.0:
        return 0.0
    return _clip_probability(2.3 * refreezing_energy - 42.0 * np.log(melting_energy + 1.0) + 3.0)


def _no_areas(prob_ice: float, surface_tw_c: float, ground_temperature_c: float) -> TypeProbabilities:
    snow = prob_ice
    freezing_drizzle = 100.0 - snow
    freezing_rain_on_ground = 0.0
    rain = 0.0
    if surface_tw_c > 0.0:
        rain = freezing_drizzle
        freezing_drizzle = 0.0
        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain
    return TypeProbabilities(
        freezing_rain_on_ground=_clip_probability(freezing_rain_on_ground),
        freezing_drizzle=_clip_probability(freezing_drizzle),
        snow=_clip_probability(snow),
        rain=_clip_probability(rain),
    )


def _snow_probability(prob_ice: float, column_melting_energy: float) -> float:
    snow_ice = _clip_probability(SNOW_PROBABILITY_PREFACTOR * np.exp(-SNOW_MELTING_ENERGY_DECAY * column_melting_energy))
    return _clip_probability(prob_ice / 100.0 * snow_ice)


def _rain_or_freezing_rain_probability(prob_ice: float, refreezing_energy: float, melting_energy: float) -> float:
    ice_probability = _clip_probability(
        FREEZING_RAIN_REFREEZE_COEFF * refreezing_energy
        + FREEZING_RAIN_MELT_COEFF * melting_energy
        + FREEZING_RAIN_INTERCEPT
    )
    if melting_energy < FREEZING_RAIN_LOW_MELT_THRESHOLD_JKG:
        ice_probability *= FREEZING_RAIN_MELT_COEFF * melting_energy
    return _clip_probability((100.0 - prob_ice) + prob_ice / 100.0 * ice_probability)


def _areas_to_probabilities(
    energies: list[float],
    column_melting_energy: float,
    prob_ice: float,
    surface_tw_c: float,
    ground_temperature_c: float,
) -> TypeProbabilities:
    if not energies:
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
            if len(energies) >= 2 and energies[1] > 0.0:
                melting_energy = energies[1]
        elif energies[0] > 0.0:
            melting_energy = energies[0]
            refreezing_energy = SHALLOW_SURFACE_REFREEZING_JKG
        if melting_energy <= 0.0:
            return _no_areas(prob_ice, surface_tw_c, ground_temperature_c)
        snow = _snow_probability(prob_ice, column_melting_energy)
        ice_pellets = _clip_probability(prob_ice / 100.0 * _ice_pellet_probability(refreezing_energy, melting_energy))
        if refreezing_energy > LARGE_REFREEZING_SNOW_THRESHOLD_JKG:
            snow = max(snow, ice_pellets)
            ice_pellets = 0.0
        freezing_rain = _rain_or_freezing_rain_probability(prob_ice, refreezing_energy, melting_energy)
    else:
        surface_melting_energy = energies[0] if energies[0] > 0.0 else 0.0
        refreezing_energy = abs(energies[1]) if len(energies) >= 2 and energies[1] < 0.0 else 0.0
        upper_melting_energy = energies[2] if len(energies) >= 3 and energies[2] > 0.0 else 0.0
        if surface_melting_energy <= 0.0:
            return _no_areas(prob_ice, surface_tw_c, ground_temperature_c)
        if refreezing_energy > 0.0 and upper_melting_energy > 0.0:
            ice_pellets = _clip_probability(prob_ice / 100.0 * _ice_pellet_probability(refreezing_energy, upper_melting_energy))
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
            rain = max(
                _rain_or_freezing_rain_probability(prob_ice, refreezing_energy, upper_melting_energy),
                melted_ice_pellets,
            )
            snow = _snow_probability(prob_ice, column_melting_energy)
        else:
            snow = _snow_probability(prob_ice, column_melting_energy)
            rain = 100.0 - snow
        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain
    return TypeProbabilities(
        freezing_rain=_clip_probability(freezing_rain),
        freezing_rain_on_ground=_clip_probability(freezing_rain_on_ground),
        ice_pellets=_clip_probability(ice_pellets),
        snow=_clip_probability(snow),
        rain=_clip_probability(rain),
    )


def refine_icon_precip_type(
    core_code: PrecipitationTypeCode,
    surface_tw_c: float,
    rates: SurfacePrecipitationRates,
) -> PrecipitationTypeCode:
    """Apply ICON's explicit surface-hydrometeor categorical refinements."""

    if core_code == PrecipitationTypeCode.NO_PRECIP:
        return core_code
    rain = max(0.0, rates.rain_kg_m2_s)
    snow = max(0.0, rates.snow_kg_m2_s)
    graupel = max(0.0, rates.graupel_kg_m2_s)
    hail = max(0.0, rates.hail_kg_m2_s)
    total = rain + snow + graupel + hail
    if total <= PTYPE_RATE_MIN_KG_M2_S:
        return core_code
    rain_fraction = rain / total
    snow_fraction = snow / total
    graupel_fraction = graupel / total
    hail_fraction = hail / total
    if hail >= PTYPE_RATE_MIN_KG_M2_S and hail_fraction >= PTYPE_DOMINANT_FRACTION:
        return PrecipitationTypeCode.HAIL
    if graupel >= PTYPE_RATE_MIN_KG_M2_S and graupel_fraction >= PTYPE_DOMINANT_FRACTION:
        return PrecipitationTypeCode.GRAUPEL
    if core_code in {
        PrecipitationTypeCode.FREEZING_RAIN,
        PrecipitationTypeCode.FREEZING_DRIZZLE,
        PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND,
        PrecipitationTypeCode.ICE_PELLETS,
    }:
        return core_code
    rain_material = rain >= PTYPE_RATE_MIN_KG_M2_S and rain_fraction >= PTYPE_MIX_FRACTION_MIN
    snow_material = snow >= PTYPE_RATE_MIN_KG_M2_S and snow_fraction >= PTYPE_MIX_FRACTION_MIN
    if rain_material and snow_material:
        return PrecipitationTypeCode.RAIN_SNOW_MIXED
    if (
        core_code == PrecipitationTypeCode.SNOW
        and snow >= PTYPE_RATE_MIN_KG_M2_S
        and not rain_material
        and PTYPE_WET_SNOW_TW_MIN_C <= surface_tw_c <= PTYPE_WET_SNOW_TW_MAX_C
    ):
        return PrecipitationTypeCode.WET_SNOW
    return core_code


def diagnose_icon_column_from_thermodynamics(
    thermodynamics: ThermodynamicColumn,
    full_level_height_m: np.ndarray | Iterable[float],
    half_level_height_m: np.ndarray | Iterable[float],
    total_precip_mm: float,
    ground_temperature_c: float,
    *,
    surface_rates: SurfacePrecipitationRates = ZERO_SURFACE_PRECIPITATION_RATES,
    precip_mask_threshold_mm: float = PTYPE_TRACE_RATE_THRESHOLD_MM_H,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> IconColumnDiagnostics:
    """Diagnose one column with the ICON-adapted thermodynamic and refinement logic."""

    temperature_c = _as_1d(thermodynamics.temperature_c, "temperature_c")
    wet_bulb_c = _as_1d(thermodynamics.wet_bulb_c, "wet_bulb_c")
    relative_humidity_ice_pct = _as_1d(thermodynamics.relative_humidity_ice_pct, "relative_humidity_ice_pct")
    full_level_height_m = _as_1d(full_level_height_m, "full_level_height_m")
    half_level_height_m = _as_1d(half_level_height_m, "half_level_height_m")
    if not (temperature_c.size == wet_bulb_c.size == relative_humidity_ice_pct.size == full_level_height_m.size):
        raise ValueError("All full-level input arrays must have identical length")
    if half_level_height_m.size != temperature_c.size + 1:
        raise ValueError("half_level_height_m must contain one more level than full-level arrays")
    if total_precip_mm <= precip_mask_threshold_mm:
        empty = TypeProbabilities()
        return IconColumnDiagnostics(0.0, 0.0, empty, empty, PrecipitationTypeCode.NO_PRECIP, PrecipitationTypeCode.NO_PRECIP, 0.0)

    start = _profile_start(full_level_height_m, vertical_cutoff_m)
    layers = _build_layers(relative_humidity_ice_pct, temperature_c, half_level_height_m, start)
    strict_prob_ice, strict_has_generation = _prob_ice(layers, relaxed=False)
    energies, column_melting_energy = _energies_surface_up(wet_bulb_c, full_level_height_m, start)
    surface_tw_c = float(wet_bulb_c[-1])
    strict_probabilities = (
        _areas_to_probabilities(energies, column_melting_energy, strict_prob_ice, surface_tw_c, ground_temperature_c)
        if strict_has_generation
        else TypeProbabilities()
    )
    probabilities = strict_probabilities
    prob_ice = strict_prob_ice
    if probabilities.categorical_code() == PrecipitationTypeCode.NO_PRECIP:
        prob_ice, has_generation = _prob_ice(layers, relaxed=True)
        probabilities = (
            _areas_to_probabilities(energies, column_melting_energy, prob_ice, surface_tw_c, ground_temperature_c)
            if has_generation
            else TypeProbabilities()
        )
    core_code = probabilities.categorical_code()
    refined_code = refine_icon_precip_type(core_code, surface_tw_c, surface_rates)
    return IconColumnDiagnostics(
        strict_prob_ice=strict_prob_ice,
        prob_ice=prob_ice,
        strict_probabilities=strict_probabilities,
        probabilities=probabilities,
        core_categorical_code=core_code,
        categorical_code=refined_code,
        surface_wet_bulb_c=surface_tw_c,
    )


def diagnose_icon_column(
    profile: IconColumnProfile,
    *,
    precip_mask_threshold_mm: float = PTYPE_TRACE_RATE_THRESHOLD_MM_H,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> IconColumnDiagnostics:
    """Diagnose one raw model column with ICON-like behavior."""

    thermodynamics = calculate_icon_thermodynamics(
        _as_1d(profile.temperature_k, "temperature_k"),
        _as_1d(profile.specific_humidity, "specific_humidity"),
        _as_1d(profile.pressure_pa, "pressure_pa"),
    )
    return diagnose_icon_column_from_thermodynamics(
        thermodynamics,
        profile.full_level_height_m,
        profile.half_level_height_m,
        profile.total_precip_mm,
        profile.ground_temperature_c,
        surface_rates=profile.surface_rates,
        precip_mask_threshold_mm=precip_mask_threshold_mm,
        vertical_cutoff_m=vertical_cutoff_m,
    )
