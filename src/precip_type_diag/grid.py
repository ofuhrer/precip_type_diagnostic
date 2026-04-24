"""Grid-level application of the single-column diagnostic."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .numba_backend import diagnose_grid_categorical_numba_kernel
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


def _prepare_grid(inputs: GridInputs) -> tuple[
    tuple[int, ...],
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
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
    return flat_shape, temperature_2d, pressure_2d, humidity_2d, full_level_height_m, precip_flat, ground_flat


def diagnose_grid(
    inputs: GridInputs,
    *,
    precip_mask_threshold_mm: float = 0.0,
) -> tuple[np.ndarray, list[ColumnDiagnostics]]:
    """Diagnose all columns of one member/hour grid.

    The thermodynamic preprocessing is vectorized over the full grid, while the
    irregular layer/area logic remains column-based to mirror the thesis prototype.
    """

    flat_shape, temperature_2d, pressure_2d, humidity_2d, full_level_height_m, precip_flat, ground_flat = _prepare_grid(
        inputs
    )
    thermodynamics = calculate_thermodynamics(temperature_2d, humidity_2d, pressure_2d)
    n_columns = precip_flat.size

    diagnostics: list[ColumnDiagnostics] = []
    categorical = np.zeros(n_columns, dtype=np.int32)
    for index in range(n_columns):
        column_diag = diagnose_column_from_thermodynamics(
            thermodynamics=ThermodynamicColumn(
                temperature_c=thermodynamics.temperature_c[:, index],
                wet_bulb_c=thermodynamics.wet_bulb_c[:, index],
                relative_humidity_ice_pct=thermodynamics.relative_humidity_ice_pct[:, index],
            ),
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
) -> np.ndarray:
    """Diagnose only the categorical microphysics-consistent class.

    This is the production path used by the CLI. It preserves the thesis logic for
    precipitating columns while skipping dry columns, which always map to code 0 in
    the microphysics-consistent output.
    """

    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    (
        flat_shape,
        temperature_2d,
        pressure_2d,
        humidity_2d,
        full_level_height_m,
        precip_flat,
        ground_flat,
    ) = _prepare_grid(inputs)

    active = np.flatnonzero(precip_flat > precip_mask_threshold_mm)
    categorical = np.zeros(precip_flat.size, dtype=np.int32)
    if active.size == 0:
        return categorical.reshape(flat_shape)

    for start in range(0, active.size, chunk_size):
        stop = min(start + chunk_size, active.size)
        indices = active[start:stop]
        thermodynamics = calculate_thermodynamics(
            temperature_2d[:, indices],
            humidity_2d[:, indices],
            pressure_2d[:, indices],
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

    return categorical.reshape(flat_shape)
