"""Small NetCDF helpers for diagnostic sidecars and ensemble products."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import netCDF4
import numpy as np


def _as_array(name: str, values: np.ndarray) -> np.ndarray:
    array = np.asarray(values)
    if array.ndim < 1:
        raise ValueError(f"{name} must be at least one-dimensional")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must contain only finite values")
    return array


def _dims_for_shape(shape: tuple[int, ...]) -> tuple[str, ...]:
    if len(shape) == 1:
        return ("cell",)
    if len(shape) == 2:
        return ("y", "x")
    return tuple(f"dim_{index}" for index in range(len(shape)))


def write_netcdf(
    path: Path,
    variables: dict[str, np.ndarray],
    *,
    attrs: dict[str, object] | None = None,
    variable_attrs: dict[str, dict[str, object]] | None = None,
) -> None:
    if not variables:
        raise ValueError("At least one NetCDF variable is required")

    prepared: dict[str, np.ndarray] = {}
    expected_shape: tuple[int, ...] | None = None
    for name, values in variables.items():
        array = _as_array(name, values)
        if expected_shape is None:
            expected_shape = tuple(array.shape)
        elif tuple(array.shape) != expected_shape:
            raise ValueError(f"{name} has shape {array.shape}; expected {expected_shape}")
        prepared[name] = array

    if expected_shape is None:
        raise ValueError("At least one NetCDF variable is required")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp.nc", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        dims = _dims_for_shape(expected_shape)
        with netCDF4.Dataset(tmp_path, "w", format="NETCDF4") as dataset:
            for dim_name, size in zip(dims, expected_shape):
                dataset.createDimension(dim_name, size)
            for key, value in (attrs or {}).items():
                dataset.setncattr(key, value)
            for name, array in prepared.items():
                dtype = "i4" if np.issubdtype(array.dtype, np.integer) else "f4"
                variable = dataset.createVariable(name, dtype, dims, zlib=True, complevel=1)
                variable[:] = array.astype(np.int32 if dtype == "i4" else np.float32, copy=False)
                for key, value in (variable_attrs or {}).get(name, {}).items():
                    variable.setncattr(key, value)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def read_netcdf(path: Path) -> dict[str, np.ndarray]:
    with netCDF4.Dataset(path, "r") as dataset:
        return {name: np.asarray(variable[:]) for name, variable in dataset.variables.items()}
