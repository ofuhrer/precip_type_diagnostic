"""Grid-level application of the single-column diagnostic."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .constants import (
    ALGORITHM_FIRDEWSA,
    ALGORITHM_ICON,
    DEFAULT_VERTICAL_CUTOFF_M,
    DIAGNOSTIC_ALGORITHMS,
    PROBABILITY_TYPE_FIELDS,
)
from .icon_numba_backend import diagnose_icon_grid_categorical_numba_kernel, diagnose_icon_grid_probabilities_numba_kernel
from .icon_profile import (
    IconColumnDiagnostics,
    SurfacePrecipitationRates,
    calculate_icon_thermodynamics,
    diagnose_icon_column_from_thermodynamics,
)
from .numba_backend import diagnose_grid_categorical_numba_kernel, diagnose_grid_probabilities_numba_kernel
from .profile import (
    ColumnDiagnostics,
    ThermodynamicColumn,
    calculate_thermodynamics,
    diagnose_column_from_thermodynamics,
)


@dataclass(frozen=True)
class GridInputs:
    temperature_k: np.ndarray
    pressure_pa: np.ndarray
    specific_humidity: np.ndarray
    half_level_height_m: np.ndarray
    total_precip_mm: np.ndarray
    ground_temperature_c: np.ndarray
    rain_rate_kg_m2_s: np.ndarray | None = None
    snow_rate_kg_m2_s: np.ndarray | None = None
    graupel_rate_kg_m2_s: np.ndarray | None = None
    hail_rate_kg_m2_s: np.ndarray | None = None


class GridDataQualityError(ValueError):
    """Raised when active columns contain unusable input data."""


@dataclass(frozen=True)
class GridQualityReport:
    total_columns: int
    active_columns: int
    invalid_total_precip_columns: int
    invalid_ground_temperature_columns: int
    invalid_profile_columns: int
    invalid_active_ground_temperature_columns: int
    invalid_active_profile_columns: int

    def as_dict(self) -> dict[str, int]:
        return {
            "total_columns": self.total_columns,
            "active_columns": self.active_columns,
            "invalid_total_precip_columns": self.invalid_total_precip_columns,
            "invalid_ground_temperature_columns": self.invalid_ground_temperature_columns,
            "invalid_profile_columns": self.invalid_profile_columns,
            "invalid_active_ground_temperature_columns": self.invalid_active_ground_temperature_columns,
            "invalid_active_profile_columns": self.invalid_active_profile_columns,
        }


@dataclass(frozen=True)
class GridCategoricalResult:
    categorical: np.ndarray
    quality: GridQualityReport


@dataclass(frozen=True)
class GridDiagnosticResult:
    categorical: np.ndarray
    probabilities: dict[str, np.ndarray]
    quality: GridQualityReport


def _prepare_grid(inputs: GridInputs) -> tuple[
    tuple[int, ...],
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
]:
    temperature_k = np.asarray(inputs.temperature_k, dtype=float)
    pressure_pa = np.asarray(inputs.pressure_pa, dtype=float)
    specific_humidity = np.asarray(inputs.specific_humidity, dtype=float)
    half_level_height_m = np.asarray(inputs.half_level_height_m, dtype=float)
    total_precip_mm = np.asarray(inputs.total_precip_mm, dtype=float)
    ground_temperature_c = np.asarray(inputs.ground_temperature_c, dtype=float)

    if temperature_k.ndim < 2:
        raise ValueError(
            "temperature_k must have shape (level, npoint) or (level, y, x), "
            f"got {temperature_k.shape}"
        )
    if pressure_pa.shape != temperature_k.shape or specific_humidity.shape != temperature_k.shape:
        raise ValueError("temperature_k, pressure_pa, and specific_humidity must have matching shapes")
    if half_level_height_m.ndim != temperature_k.ndim:
        raise ValueError("half_level_height_m must have the same number of dimensions as the full-level fields")
    if half_level_height_m.shape[0] != temperature_k.shape[0] + 1 or half_level_height_m.shape[1:] != temperature_k.shape[1:]:
        raise ValueError("half_level_height_m must have one more vertical level than the full-level fields")
    if total_precip_mm.shape != temperature_k.shape[1:] or ground_temperature_c.shape != temperature_k.shape[1:]:
        raise ValueError("Surface fields must match the horizontal shape of the 3D fields")

    flat_shape = temperature_k.shape[1:]
    n_columns = int(np.prod(flat_shape))

    temperature_2d = temperature_k.reshape(temperature_k.shape[0], n_columns)
    pressure_2d = pressure_pa.reshape(pressure_pa.shape[0], n_columns)
    humidity_2d = specific_humidity.reshape(specific_humidity.shape[0], n_columns)
    half_level_height_2d = half_level_height_m.reshape(half_level_height_m.shape[0], n_columns)
    full_level_height_m = 0.5 * (half_level_height_2d[:-1] + half_level_height_2d[1:])
    precip_flat = total_precip_mm.reshape(n_columns)
    ground_flat = ground_temperature_c.reshape(n_columns)

    def prepare_rate(values: np.ndarray | None, name: str) -> np.ndarray:
        if values is None:
            return np.zeros(n_columns, dtype=float)
        rate = np.asarray(values, dtype=float)
        if rate.shape != flat_shape:
            raise ValueError(f"{name} must match the horizontal grid shape {flat_shape}, got {rate.shape}")
        return rate.reshape(n_columns)

    rates = (
        prepare_rate(inputs.rain_rate_kg_m2_s, "rain_rate_kg_m2_s"),
        prepare_rate(inputs.snow_rate_kg_m2_s, "snow_rate_kg_m2_s"),
        prepare_rate(inputs.graupel_rate_kg_m2_s, "graupel_rate_kg_m2_s"),
        prepare_rate(inputs.hail_rate_kg_m2_s, "hail_rate_kg_m2_s"),
    )
    return (
        flat_shape,
        temperature_2d,
        pressure_2d,
        humidity_2d,
        half_level_height_2d,
        full_level_height_m,
        precip_flat,
        ground_flat,
        rates,
    )


def _finite_profile_columns(*arrays: np.ndarray) -> np.ndarray:
    finite = np.ones(arrays[0].shape[1], dtype=bool)
    for array in arrays:
        finite &= np.all(np.isfinite(array), axis=0)
    return finite


def _format_bad_indices(mask: np.ndarray) -> str:
    indices = np.flatnonzero(mask)
    preview = ",".join(str(int(index)) for index in indices[:5])
    if indices.size > 5:
        preview += ",..."
    return preview


def _validate_algorithm(algorithm: str) -> str:
    normalized = algorithm.lower()
    if normalized not in DIAGNOSTIC_ALGORITHMS:
        raise ValueError(f"algorithm must be one of {DIAGNOSTIC_ALGORITHMS}, got {algorithm!r}")
    return normalized


def _quality_report(
    temperature_2d: np.ndarray,
    pressure_2d: np.ndarray,
    humidity_2d: np.ndarray,
    full_level_height_m: np.ndarray,
    precip_flat: np.ndarray,
    ground_flat: np.ndarray,
    precip_mask_threshold_mm: float,
) -> tuple[GridQualityReport, np.ndarray]:
    precip_finite = np.isfinite(precip_flat)
    ground_finite = np.isfinite(ground_flat)
    profile_finite = _finite_profile_columns(temperature_2d, pressure_2d, humidity_2d, full_level_height_m)
    active = precip_finite & (precip_flat > precip_mask_threshold_mm)

    quality = GridQualityReport(
        total_columns=int(precip_flat.size),
        active_columns=int(np.count_nonzero(active)),
        invalid_total_precip_columns=int(np.count_nonzero(~precip_finite)),
        invalid_ground_temperature_columns=int(np.count_nonzero(~ground_finite)),
        invalid_profile_columns=int(np.count_nonzero(~profile_finite)),
        invalid_active_ground_temperature_columns=int(np.count_nonzero(active & ~ground_finite)),
        invalid_active_profile_columns=int(np.count_nonzero(active & ~profile_finite)),
    )
    return quality, active


def _raise_for_bad_active_data(quality: GridQualityReport, active: np.ndarray, ground_flat: np.ndarray, profile_finite: np.ndarray | None = None) -> None:
    if quality.invalid_total_precip_columns:
        raise GridDataQualityError(
            "total_precip_mm contains non-finite values in "
            f"{quality.invalid_total_precip_columns} column(s)"
        )
    if quality.invalid_active_ground_temperature_columns:
        bad = active & ~np.isfinite(ground_flat)
        raise GridDataQualityError(
            "ground_temperature_c contains non-finite values in active precipitation column(s): "
            f"{_format_bad_indices(bad)}"
        )
    if quality.invalid_active_profile_columns:
        if profile_finite is None:
            bad_text = f"{quality.invalid_active_profile_columns} column(s)"
        else:
            bad_text = _format_bad_indices(active & ~profile_finite)
        raise GridDataQualityError(
            "temperature, pressure, humidity, or height contains non-finite values in active precipitation column(s): "
            f"{bad_text}"
        )


def _raise_for_bad_icon_active_data(
    active: np.ndarray,
    temperature_2d: np.ndarray,
    pressure_2d: np.ndarray,
    half_level_height_2d: np.ndarray,
    rates: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
) -> None:
    physical_profile = np.all(temperature_2d > 0.0, axis=0) & np.all(pressure_2d > 0.0, axis=0)
    descending_interfaces = np.all(half_level_height_2d[:-1] > half_level_height_2d[1:], axis=0)
    bad_profile = active & ~(physical_profile & descending_interfaces)
    if np.any(bad_profile):
        raise GridDataQualityError(
            "ICON mode requires positive temperature/pressure and strictly descending half-level heights in active "
            f"precipitation column(s): {_format_bad_indices(bad_profile)}"
        )
    rate_finite = np.ones(active.size, dtype=bool)
    for rate in rates:
        rate_finite &= np.isfinite(rate)
    bad_rates = active & ~rate_finite
    if np.any(bad_rates):
        raise GridDataQualityError(
            "ICON surface microphysics rates contain non-finite values in active precipitation column(s): "
            f"{_format_bad_indices(bad_rates)}"
        )


def diagnose_grid(
    inputs: GridInputs,
    *,
    precip_mask_threshold_mm: float = 0.0,
    algorithm: str = ALGORITHM_FIRDEWSA,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> tuple[np.ndarray, list[ColumnDiagnostics | IconColumnDiagnostics]]:
    """Diagnose all columns of one member/hour grid.

    The thermodynamic preprocessing is vectorized over the full grid, while the
    irregular layer/area logic remains column-based to mirror the thesis prototype.
    """

    algorithm = _validate_algorithm(algorithm)
    (
        flat_shape,
        temperature_2d,
        pressure_2d,
        humidity_2d,
        half_level_height_2d,
        full_level_height_m,
        precip_flat,
        ground_flat,
        rates,
    ) = _prepare_grid(inputs)
    thermodynamics = (
        calculate_icon_thermodynamics(temperature_2d, humidity_2d, pressure_2d)
        if algorithm == ALGORITHM_ICON
        else calculate_thermodynamics(temperature_2d, humidity_2d, pressure_2d)
    )
    n_columns = precip_flat.size

    diagnostics: list[ColumnDiagnostics | IconColumnDiagnostics] = []
    categorical = np.zeros(n_columns, dtype=np.int32)
    for index in range(n_columns):
        column_thermodynamics = ThermodynamicColumn(
            temperature_c=thermodynamics.temperature_c[:, index],
            wet_bulb_c=thermodynamics.wet_bulb_c[:, index],
            relative_humidity_ice_pct=thermodynamics.relative_humidity_ice_pct[:, index],
        )
        column_diag: ColumnDiagnostics | IconColumnDiagnostics
        if algorithm == ALGORITHM_ICON:
            column_diag = diagnose_icon_column_from_thermodynamics(
                thermodynamics=column_thermodynamics,
                full_level_height_m=full_level_height_m[:, index],
                half_level_height_m=half_level_height_2d[:, index],
                total_precip_mm=float(precip_flat[index]),
                ground_temperature_c=float(ground_flat[index]),
                surface_rates=SurfacePrecipitationRates(
                    rain_kg_m2_s=float(rates[0][index]),
                    snow_kg_m2_s=float(rates[1][index]),
                    graupel_kg_m2_s=float(rates[2][index]),
                    hail_kg_m2_s=float(rates[3][index]),
                ),
                precip_mask_threshold_mm=precip_mask_threshold_mm,
                vertical_cutoff_m=vertical_cutoff_m,
            )
        else:
            column_diag = diagnose_column_from_thermodynamics(
                thermodynamics=column_thermodynamics,
                full_level_height_m=full_level_height_m[:, index],
                total_precip_mm=float(precip_flat[index]),
                ground_temperature_c=float(ground_flat[index]),
                precip_mask_threshold_mm=precip_mask_threshold_mm,
            )
        diagnostics.append(column_diag)
        categorical[index] = int(column_diag.categorical_code)

    return categorical.reshape(flat_shape), diagnostics


def diagnose_grid_categorical(
    inputs: GridInputs,
    *,
    chunk_size: int = 4096,
    precip_mask_threshold_mm: float = 0.0,
    algorithm: str = ALGORITHM_FIRDEWSA,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> np.ndarray:
    """Diagnose only the categorical microphysics-consistent class.

    This is the production path used by the CLI. It preserves the thesis logic for
    precipitating columns while skipping dry columns, which always map to code 0 in
    the microphysics-consistent output.
    """

    return diagnose_grid_categorical_with_quality(
        inputs,
        chunk_size=chunk_size,
        precip_mask_threshold_mm=precip_mask_threshold_mm,
        algorithm=algorithm,
        vertical_cutoff_m=vertical_cutoff_m,
    ).categorical


def diagnose_grid_categorical_with_quality(
    inputs: GridInputs,
    *,
    chunk_size: int = 4096,
    precip_mask_threshold_mm: float = 0.0,
    algorithm: str = ALGORITHM_FIRDEWSA,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> GridCategoricalResult:
    """Diagnose categorical classes and report input data-quality counters."""

    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    algorithm = _validate_algorithm(algorithm)
    (
        flat_shape,
        temperature_2d,
        pressure_2d,
        humidity_2d,
        half_level_height_2d,
        full_level_height_m,
        precip_flat,
        ground_flat,
        rates,
    ) = _prepare_grid(inputs)

    quality, active_mask = _quality_report(
        temperature_2d,
        pressure_2d,
        humidity_2d,
        full_level_height_m,
        precip_flat,
        ground_flat,
        precip_mask_threshold_mm,
    )
    profile_finite = _finite_profile_columns(temperature_2d, pressure_2d, humidity_2d, full_level_height_m)
    _raise_for_bad_active_data(quality, active_mask, ground_flat, profile_finite)
    if algorithm == ALGORITHM_ICON:
        _raise_for_bad_icon_active_data(active_mask, temperature_2d, pressure_2d, half_level_height_2d, rates)

    active = np.flatnonzero(active_mask)
    categorical = np.zeros(precip_flat.size, dtype=np.int32)
    if active.size == 0:
        return GridCategoricalResult(categorical=categorical.reshape(flat_shape), quality=quality)

    for start in range(0, active.size, chunk_size):
        stop = min(start + chunk_size, active.size)
        indices = active[start:stop]
        if algorithm == ALGORITHM_ICON:
            thermodynamics = calculate_icon_thermodynamics(
                temperature_2d[:, indices], humidity_2d[:, indices], pressure_2d[:, indices]
            )
            categorical[indices] = diagnose_icon_grid_categorical_numba_kernel(
                thermodynamics.temperature_c,
                thermodynamics.wet_bulb_c,
                thermodynamics.relative_humidity_ice_pct,
                full_level_height_m[:, indices],
                half_level_height_2d[:, indices],
                precip_flat[indices],
                ground_flat[indices],
                rates[0][indices],
                rates[1][indices],
                rates[2][indices],
                rates[3][indices],
                precip_mask_threshold_mm,
                vertical_cutoff_m,
            )
        else:
            thermodynamics = calculate_thermodynamics(
                temperature_2d[:, indices], humidity_2d[:, indices], pressure_2d[:, indices]
            )
            categorical[indices] = diagnose_grid_categorical_numba_kernel(
                thermodynamics.temperature_c,
                thermodynamics.wet_bulb_c,
                thermodynamics.relative_humidity_ice_pct,
                full_level_height_m[:, indices],
                precip_flat[indices],
                ground_flat[indices],
                precip_mask_threshold_mm,
            )

    return GridCategoricalResult(categorical=categorical.reshape(flat_shape), quality=quality)


def diagnose_grid_probabilities_with_quality(
    inputs: GridInputs,
    *,
    chunk_size: int = 4096,
    precip_mask_threshold_mm: float = 0.0,
    algorithm: str = ALGORITHM_FIRDEWSA,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> GridDiagnosticResult:
    """Diagnose categorical classes and microphysics-consistent probabilities."""

    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    algorithm = _validate_algorithm(algorithm)
    (
        flat_shape,
        temperature_2d,
        pressure_2d,
        humidity_2d,
        half_level_height_2d,
        full_level_height_m,
        precip_flat,
        ground_flat,
        rates,
    ) = _prepare_grid(inputs)

    quality, active_mask = _quality_report(
        temperature_2d,
        pressure_2d,
        humidity_2d,
        full_level_height_m,
        precip_flat,
        ground_flat,
        precip_mask_threshold_mm,
    )
    profile_finite = _finite_profile_columns(temperature_2d, pressure_2d, humidity_2d, full_level_height_m)
    _raise_for_bad_active_data(quality, active_mask, ground_flat, profile_finite)
    if algorithm == ALGORITHM_ICON:
        _raise_for_bad_icon_active_data(active_mask, temperature_2d, pressure_2d, half_level_height_2d, rates)

    active = np.flatnonzero(active_mask)
    categorical = np.zeros(precip_flat.size, dtype=np.int32)
    probability_stack = np.zeros((len(PROBABILITY_TYPE_FIELDS), precip_flat.size), dtype=np.float64)
    if active.size == 0:
        return GridDiagnosticResult(
            categorical=categorical.reshape(flat_shape),
            probabilities={f"prob_{name}_mm": probability_stack[index].reshape(flat_shape) for index, (name, _) in enumerate(PROBABILITY_TYPE_FIELDS)},
            quality=quality,
        )

    for start in range(0, active.size, chunk_size):
        stop = min(start + chunk_size, active.size)
        indices = active[start:stop]
        if algorithm == ALGORITHM_ICON:
            thermodynamics = calculate_icon_thermodynamics(
                temperature_2d[:, indices], humidity_2d[:, indices], pressure_2d[:, indices]
            )
            chunk_categorical, chunk_probabilities = diagnose_icon_grid_probabilities_numba_kernel(
                thermodynamics.temperature_c,
                thermodynamics.wet_bulb_c,
                thermodynamics.relative_humidity_ice_pct,
                full_level_height_m[:, indices],
                half_level_height_2d[:, indices],
                precip_flat[indices],
                ground_flat[indices],
                rates[0][indices],
                rates[1][indices],
                rates[2][indices],
                rates[3][indices],
                precip_mask_threshold_mm,
                vertical_cutoff_m,
            )
        else:
            thermodynamics = calculate_thermodynamics(
                temperature_2d[:, indices], humidity_2d[:, indices], pressure_2d[:, indices]
            )
            chunk_categorical, chunk_probabilities = diagnose_grid_probabilities_numba_kernel(
                thermodynamics.temperature_c,
                thermodynamics.wet_bulb_c,
                thermodynamics.relative_humidity_ice_pct,
                full_level_height_m[:, indices],
                precip_flat[indices],
                ground_flat[indices],
                precip_mask_threshold_mm,
            )
        categorical[indices] = chunk_categorical
        probability_stack[:, indices] = chunk_probabilities

    return GridDiagnosticResult(
        categorical=categorical.reshape(flat_shape),
        probabilities={f"prob_{name}_mm": probability_stack[index].reshape(flat_shape) for index, (name, _) in enumerate(PROBABILITY_TYPE_FIELDS)},
        quality=quality,
    )
