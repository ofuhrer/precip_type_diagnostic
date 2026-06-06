"""Single-column implementation of the thesis diagnostic."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from .constants import (
    CATEGORICAL_PRIORITY,
    DEWPOINT_AW,
    DEWPOINT_BW,
    FIELDEXTRA_ZG,
    FIELDEXTRA_ZH,
    FIELDEXTRA_ZI,
    FIELDEXTRA_ZL,
    FIELDEXTRA_ZM,
    FIELDEXTRA_ZN,
    FIELDEXTRA_ZO,
    GRAVITY,
    GROUND_FREEZING_THRESHOLD_C,
    KELVIN_OFFSET,
    PRECIP_GENERATION_MIN_DEPTH_M,
    PRECIP_GENERATION_RHI_THRESHOLD_PCT,
    PROB_ICE_FULL_THRESHOLD_C,
    PROB_ICE_ZERO_THRESHOLD_C,
    SATURATION_B1,
    SATURATION_B2I,
    SATURATION_B3,
    SATURATION_B4I,
    SHALLOW_SURFACE_REFREEZING_JKG,
    SMALL_AREA_THRESHOLD_JKG,
    SUBLIMATION_MIN_DEPTH_M,
    SURFACE_LEVEL_INDEX,
    PrecipitationTypeCode,
)


def _as_1d(array: np.ndarray | Iterable[float], name: str) -> np.ndarray:
    values = np.asarray(array, dtype=float)
    if values.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {values.shape}")
    return values


def _clip_probability(value: float) -> float:
    if not np.isfinite(value):
        return 0.0
    return float(np.clip(value, 0.0, 100.0))


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


def _ice_pellet_ice_probability(refreezing_energy: float, melting_energy: float) -> float:
    if melting_energy <= -1.0:
        return 0.0
    return _clip_probability(2.3 * refreezing_energy - 42.0 * np.log(melting_energy + 1.0) + 3.0)


def calc_full_level_height_from_hhl(hhl_m: np.ndarray | Iterable[float]) -> np.ndarray:
    """Convert ICON half-level heights to full-level heights."""

    hhl = _as_1d(hhl_m, "hhl_m")
    if hhl.size < 2:
        raise ValueError("hhl_m must contain at least two half levels")
    return hhl[1:] + (hhl[:-1] - hhl[1:]) / 2.0


@dataclass(frozen=True)
class ColumnProfile:
    """Raw model-input column."""

    temperature_k: np.ndarray
    pressure_pa: np.ndarray
    specific_humidity: np.ndarray
    full_level_height_m: np.ndarray
    total_precip_mm: float
    ground_temperature_c: float


@dataclass(frozen=True)
class ThermodynamicColumn:
    """Thermodynamic column used by the diagnostic."""

    temperature_c: np.ndarray
    wet_bulb_c: np.ndarray
    relative_humidity_ice_pct: np.ndarray


@dataclass(frozen=True)
class TypeProbabilities:
    freezing_rain: float = 0.0
    freezing_rain_on_ground: float = 0.0
    ice_pellets: float = 0.0
    freezing_drizzle: float = 0.0
    snow: float = 0.0
    rain: float = 0.0

    def total(self) -> float:
        return (
            self.freezing_rain
            + self.freezing_rain_on_ground
            + self.ice_pellets
            + self.freezing_drizzle
            + self.snow
            + self.rain
        )

    def categorical_code(self) -> PrecipitationTypeCode:
        mapping = {
            "freezing_rain": PrecipitationTypeCode.FREEZING_RAIN,
            "freezing_rain_on_ground": PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND,
            "ice_pellets": PrecipitationTypeCode.ICE_PELLETS,
            "freezing_drizzle": PrecipitationTypeCode.FREEZING_DRIZZLE,
            "snow": PrecipitationTypeCode.SNOW,
            "rain": PrecipitationTypeCode.RAIN,
        }
        highest_probability = max(getattr(self, name) for name in CATEGORICAL_PRIORITY)
        if highest_probability <= 0.0:
            return PrecipitationTypeCode.NO_PRECIP
        for name in CATEGORICAL_PRIORITY:
            if getattr(self, name) == highest_probability:
                return mapping[name]
        return PrecipitationTypeCode.NO_PRECIP


@dataclass(frozen=True)
class ColumnDiagnostics:
    """Full output for one column."""

    pure_prob_ice: float
    microphysics_prob_ice: float
    pure_probabilities: TypeProbabilities
    microphysics_probabilities: TypeProbabilities
    categorical_code: PrecipitationTypeCode


@dataclass(frozen=True)
class _Layer:
    is_precipitating: bool
    start: int
    end: int
    depth_m: float
    minimum_temperature_c: float


def calculate_thermodynamics(
    temperature_k: np.ndarray,
    specific_humidity: np.ndarray,
    pressure_pa: np.ndarray,
) -> ThermodynamicColumn:
    """Vectorized thermodynamic preprocessing from the thesis prototype."""

    temperature_k = np.asarray(temperature_k, dtype=float)
    specific_humidity = np.asarray(specific_humidity, dtype=float)
    pressure_pa = np.asarray(pressure_pa, dtype=float)

    temperature_c = temperature_k - KELVIN_OFFSET

    dew_point_k = DEWPOINT_BW / np.log((DEWPOINT_AW * 0.622) / (specific_humidity * pressure_pa))

    e_ice = SATURATION_B1 * np.exp(
        SATURATION_B2I * (temperature_k - SATURATION_B3) / (temperature_k - SATURATION_B4I)
    )
    e_dew = SATURATION_B1 * np.exp(
        SATURATION_B2I * (dew_point_k - SATURATION_B3) / (dew_point_k - SATURATION_B4I)
    )
    relative_humidity_ice_pct = (e_dew / e_ice) * 100.0

    tl = temperature_c * 10.0
    tp = (dew_point_k - KELVIN_OFFSET) * 10.0
    delta_t = tl - tp
    zt = tp + FIELDEXTRA_ZG * delta_t * (FIELDEXTRA_ZH - tp / FIELDEXTRA_ZI)
    wet_bulb_c = FIELDEXTRA_ZL * (
        tp + (delta_t / (1.0 + 10.0 * FIELDEXTRA_ZM * np.exp(FIELDEXTRA_ZN * zt / (FIELDEXTRA_ZO + zt)) / pressure_pa))
    )

    return ThermodynamicColumn(
        temperature_c=temperature_c,
        wet_bulb_c=wet_bulb_c,
        relative_humidity_ice_pct=relative_humidity_ice_pct,
    )


def _contiguous_segments(values: np.ndarray) -> list[tuple[float, int, int]]:
    boundaries = np.flatnonzero(np.r_[True, values[1:] != values[:-1], True])
    return [(values[start], start, end) for start, end in zip(boundaries[:-1], boundaries[1:])]


def _build_layers(
    relative_humidity_ice_pct: np.ndarray,
    temperature_c: np.ndarray,
    full_level_height_m: np.ndarray,
) -> list[_Layer]:
    precip_mask = (relative_humidity_ice_pct > PRECIP_GENERATION_RHI_THRESHOLD_PCT).astype(int)
    layers: list[_Layer] = []
    for value, start, end in _contiguous_segments(precip_mask):
        height_slice = full_level_height_m[start:end]
        layers.append(
            _Layer(
                is_precipitating=bool(value),
                start=start,
                end=end,
                depth_m=float(np.nanmax(height_slice) - np.nanmin(height_slice)),
                minimum_temperature_c=float(np.nanmin(temperature_c[start:end])),
            )
        )
    return layers


def _prob_ice(
    layers: list[_Layer],
    *,
    relaxed_precip_layers: bool,
) -> tuple[float, bool]:
    sublimation_layers = [layer for layer in layers if (not layer.is_precipitating) and layer.depth_m > SUBLIMATION_MIN_DEPTH_M]
    precip_layers = [
        layer
        for layer in layers
        if layer.is_precipitating and (relaxed_precip_layers or layer.depth_m > PRECIP_GENERATION_MIN_DEPTH_M)
    ]

    if not precip_layers:
        if relaxed_precip_layers and layers:
            return _prob_ice_from_temperature(layers[0].minimum_temperature_c), True
        return 0.0, False

    valid_layers = [
        layer
        for layer in precip_layers
        if not any(sublimation.start > layer.start for sublimation in sublimation_layers)
    ]

    if valid_layers:
        return _prob_ice_from_temperature(min(layer.minimum_temperature_c for layer in valid_layers)), True

    if relaxed_precip_layers:
        lowest_precip_layer = max(precip_layers, key=lambda layer: layer.start)
        return _prob_ice_from_temperature(lowest_precip_layer.minimum_temperature_c), True

    return 0.0, False


def _prepare_signs(wet_bulb_c: np.ndarray) -> np.ndarray:
    signs = np.sign(wet_bulb_c)
    if np.all(signs == 0):
        return np.ones_like(signs)
    for index, value in enumerate(signs):
        if value != 0:
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
            signs[index] = 1.0
    return signs


def _segment_energy(wet_bulb_c: np.ndarray, full_level_height_m: np.ndarray, start: int, end: int) -> float:
    if end - start < 2:
        return 0.0
    mean_tw_c = 0.5 * (wet_bulb_c[start : end - 1] + wet_bulb_c[start + 1 : end])
    dz = full_level_height_m[start : end - 1] - full_level_height_m[start + 1 : end]
    return float(np.nansum((mean_tw_c / KELVIN_OFFSET) * dz * GRAVITY))


def _energies_surface_up(
    wet_bulb_c: np.ndarray,
    full_level_height_m: np.ndarray,
) -> list[float]:
    segments = [
        (sign, start, end, _segment_energy(wet_bulb_c, full_level_height_m, start, end))
        for sign, start, end in _contiguous_segments(_prepare_signs(wet_bulb_c))
    ]

    # Thesis section 3.1.2: the topmost area down to the first freezing level is ignored.
    candidate_segments = segments[1:]

    filtered: list[tuple[float, int, int, float]] = []
    for sign, start, end, energy in candidate_segments:
        if abs(energy) < SMALL_AREA_THRESHOLD_JKG:
            continue
        filtered.append((sign, start, end, energy))

    merged: list[list[float | int]] = []
    for sign, start, end, energy in filtered:
        if merged and np.sign(float(merged[-1][3])) == np.sign(energy):
            merged[-1][2] = end
            merged[-1][3] = float(merged[-1][3]) + energy
        else:
            merged.append([sign, start, end, energy])

    energies = [float(item[3]) for item in reversed(merged)]

    if wet_bulb_c[SURFACE_LEVEL_INDEX] < 0.0:
        return energies[:2]
    return energies[:3]


def _no_areas(prob_ice: float, surface_tw_c: float) -> TypeProbabilities:
    snow = prob_ice
    freezing_drizzle = 100.0 - snow
    rain = 0.0
    if surface_tw_c > 0.0:
        rain = freezing_drizzle
        freezing_drizzle = 0.0
    return TypeProbabilities(
        snow=_clip_probability(snow),
        freezing_drizzle=_clip_probability(freezing_drizzle),
        rain=_clip_probability(rain),
    )


def _areas_to_probabilities(
    energies: list[float],
    prob_ice: float,
    surface_tw_c: float,
    ground_temperature_c: float,
) -> TypeProbabilities:
    if not energies:
        return _no_areas(prob_ice, surface_tw_c)

    freezing_rain = 0.0
    freezing_rain_on_ground = 0.0
    ice_pellets = 0.0
    freezing_drizzle = 0.0
    snow = 0.0
    rain = 0.0

    if len(energies) == 1:
        melting_energy = energies[0]
        snow_ice = _clip_probability(1540.0 * np.exp(-0.29 * melting_energy))
        snow = _clip_probability((prob_ice / 100.0) * snow_ice)
        rain = 100.0 - snow
        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain
        if surface_tw_c < 0.0:
            refreezing_energy = SHALLOW_SURFACE_REFREEZING_JKG
            fzra_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
            if melting_energy < 5.0:
                fzra_ice *= 0.2 * melting_energy
            freezing_rain = _clip_probability((100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(fzra_ice))

            ice_pellet_ice = _ice_pellet_ice_probability(refreezing_energy, melting_energy)
            ice_pellets = _clip_probability((prob_ice / 100.0) * ice_pellet_ice)
            rain = 0.0
            freezing_rain_on_ground = 0.0

    elif len(energies) >= 2 and len(energies) % 2 == 0:
        melting_energy = energies[1]
        refreezing_energy = abs(energies[0])

        snow_ice = _clip_probability(1540.0 * np.exp(-0.28 * melting_energy))
        snow = _clip_probability((prob_ice / 100.0) * snow_ice)

        ice_pellet_ice = _ice_pellet_ice_probability(refreezing_energy, melting_energy)
        ice_pellets = _clip_probability((prob_ice / 100.0) * ice_pellet_ice)

        fzra_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
        if melting_energy < 5.0:
            fzra_ice *= 0.2 * melting_energy
        freezing_rain = _clip_probability((100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(fzra_ice))

        if surface_tw_c > 0.0:
            freezing_rain = 0.0

    else:
        upper_melting_energy = energies[2]
        surface_melting_energy = energies[0]
        total_melting_energy = upper_melting_energy + surface_melting_energy
        refreezing_energy = abs(energies[1])

        ice_pellet_ice = _ice_pellet_ice_probability(refreezing_energy, upper_melting_energy)
        ice_pellets = _clip_probability((prob_ice / 100.0) * ice_pellet_ice)

        rain_ice = -2.1 * refreezing_energy + 0.2 * upper_melting_energy + 458.0
        if upper_melting_energy < 5.0:
            rain_ice *= 0.2 * upper_melting_energy
        rain = _clip_probability((100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(rain_ice))

        snow_ice = _clip_probability(1540.0 * np.exp(-0.28 * total_melting_energy))
        snow = _clip_probability((prob_ice / 100.0) * snow_ice)

        if ground_temperature_c < GROUND_FREEZING_THRESHOLD_C:
            freezing_rain_on_ground = rain

        if surface_tw_c < 0.0:
            refreezing_energy = SHALLOW_SURFACE_REFREEZING_JKG
            melting_energy = energies[0]
            fzra_ice = -2.1 * refreezing_energy + 0.2 * melting_energy + 458.0
            if melting_energy < 5.0:
                fzra_ice *= 0.2 * melting_energy
            freezing_rain = _clip_probability((100.0 - prob_ice) + (prob_ice / 100.0) * _clip_probability(fzra_ice))
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


def diagnose_column_from_thermodynamics(
    thermodynamics: ThermodynamicColumn,
    full_level_height_m: np.ndarray | Iterable[float],
    total_precip_mm: float,
    ground_temperature_c: float,
    precip_mask_threshold_mm: float = 0.0,
) -> ColumnDiagnostics:
    """Diagnose one column from precomputed thermodynamic fields."""

    temperature_c = _as_1d(thermodynamics.temperature_c, "temperature_c")
    wet_bulb_c = _as_1d(thermodynamics.wet_bulb_c, "wet_bulb_c")
    relative_humidity_ice_pct = _as_1d(thermodynamics.relative_humidity_ice_pct, "relative_humidity_ice_pct")
    full_level_height_m = _as_1d(np.asarray(full_level_height_m, dtype=float), "full_level_height_m")

    if not (
        temperature_c.size
        == wet_bulb_c.size
        == relative_humidity_ice_pct.size
        == full_level_height_m.size
    ):
        raise ValueError("All full-level input arrays must have identical length")

    layers = _build_layers(relative_humidity_ice_pct, temperature_c, full_level_height_m)
    pure_prob_ice, pure_has_precip_generation = _prob_ice(layers, relaxed_precip_layers=False)
    energies = _energies_surface_up(wet_bulb_c, full_level_height_m)
    if pure_has_precip_generation:
        pure_probabilities = _areas_to_probabilities(
            energies,
            pure_prob_ice,
            wet_bulb_c[SURFACE_LEVEL_INDEX],
            ground_temperature_c,
        )
    else:
        pure_probabilities = TypeProbabilities()

    if total_precip_mm <= precip_mask_threshold_mm:
        microphysics_prob_ice = pure_prob_ice
        microphysics_probabilities = TypeProbabilities()
    else:
        if pure_probabilities.total() == 0.0:
            microphysics_prob_ice, microphysics_has_precip_generation = _prob_ice(layers, relaxed_precip_layers=True)
            if microphysics_has_precip_generation:
                microphysics_probabilities = _areas_to_probabilities(
                    energies,
                    microphysics_prob_ice,
                    wet_bulb_c[SURFACE_LEVEL_INDEX],
                    ground_temperature_c,
                )
            else:
                microphysics_probabilities = TypeProbabilities()
        else:
            microphysics_prob_ice = pure_prob_ice
            microphysics_probabilities = pure_probabilities

    return ColumnDiagnostics(
        pure_prob_ice=pure_prob_ice,
        microphysics_prob_ice=microphysics_prob_ice,
        pure_probabilities=pure_probabilities,
        microphysics_probabilities=microphysics_probabilities,
        categorical_code=microphysics_probabilities.categorical_code(),
    )


def diagnose_column(
    profile: ColumnProfile,
    *,
    precip_mask_threshold_mm: float = 0.0,
) -> ColumnDiagnostics:
    """Diagnose one raw model column."""

    thermodynamics = calculate_thermodynamics(
        temperature_k=_as_1d(profile.temperature_k, "temperature_k"),
        specific_humidity=_as_1d(profile.specific_humidity, "specific_humidity"),
        pressure_pa=_as_1d(profile.pressure_pa, "pressure_pa"),
    )
    return diagnose_column_from_thermodynamics(
        thermodynamics=thermodynamics,
        full_level_height_m=profile.full_level_height_m,
        total_precip_mm=float(profile.total_precip_mm),
        ground_temperature_c=float(profile.ground_temperature_c),
        precip_mask_threshold_mm=precip_mask_threshold_mm,
    )
