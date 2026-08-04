"""NetCDF member PTYPE outputs and ensemble probability products."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .constants import (
    ALGORITHM_FIRDEWSA,
    CATEGORICAL_PROBABILITY_CODES,
    CATEGORICAL_PROBABILITY_CODES_BY_ALGORITHM,
    DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM,
    DEFAULT_PROBABILITY_THRESHOLD_PERCENT,
    FINAL_PROBABILITY_VARIABLES,
    MEMBER_DIAGNOSTIC_VARIABLES,
    PROBABILITY_TYPE_FIELDS,
    PrecipitationTypeCode,
)
from .netcdfio import read_netcdf, write_netcdf

PROBABILITY_PRODUCT_NAMES = FINAL_PROBABILITY_VARIABLES


def _categorical_codes(algorithm: str) -> tuple[PrecipitationTypeCode, ...]:
    normalized = algorithm.lower()
    try:
        return CATEGORICAL_PROBABILITY_CODES_BY_ALGORITHM[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported diagnostic algorithm {algorithm!r}") from exc


def probability_product_names(algorithm: str = ALGORITHM_FIRDEWSA) -> tuple[str, ...]:
    return (
        *(f"prob_{name}_mm_ens" for name, _ in PROBABILITY_TYPE_FIELDS),
        *(f"precip_{name}_th_ens" for name, _ in PROBABILITY_TYPE_FIELDS),
        *(f"ptype_probability_{int(code)}" for code in _categorical_codes(algorithm)),
        "valid_member_count",
        "hourly_precip_mean_mm",
    )


@dataclass(frozen=True)
class StepProbabilityProducts:
    probability_means: dict[str, np.ndarray]
    thresholded_precip_means: dict[str, np.ndarray]
    categorical_probabilities: dict[int, np.ndarray]
    valid_member_count: np.ndarray
    hourly_precip_mean_mm: np.ndarray


class ProbabilityProductError(RuntimeError):
    """Raised when strict probability-product generation cannot complete."""


def step_token(step: int) -> str:
    days, hours = divmod(step, 24)
    return f"{days:02d}{hours:02d}0000"


def member_grib_path(output_root: Path, model: str, date: str, time_value: str, member: str, step: int) -> Path:
    return output_root / model / date / time_value / member / f"lfff{step_token(step)}.ptype.grib2"


def member_netcdf_path(output_root: Path, model: str, date: str, time_value: str, member: str, step: int) -> Path:
    return output_root / model / date / time_value / member / f"lfff{step_token(step)}.ptype.nc"


def probability_output_dir(output_root: Path, model: str, date: str, time_value: str) -> Path:
    return output_root / model / date / time_value / "probabilities"


def probability_output_path(output_root: Path, model: str, date: str, time_value: str, step: int) -> Path:
    return probability_output_dir(output_root, model, date, time_value) / f"lfff{step_token(step)}.ptype_prob.nc"


def _validate_same_shape(name: str, values: np.ndarray, expected_shape: tuple[int, ...] | None) -> tuple[int, ...]:
    array = np.asarray(values)
    if expected_shape is None:
        return tuple(array.shape)
    if tuple(array.shape) != expected_shape:
        raise ValueError(f"{name} has shape {array.shape}; expected {expected_shape}")
    return expected_shape


def _validate_probability(name: str, values: np.ndarray) -> None:
    if not np.all(np.isfinite(values)):
        raise ValueError(f"{name} must contain only finite values")
    if np.nanmin(values) < 0.0 or np.nanmax(values) > 100.0:
        raise ValueError(f"{name} must be in percent range 0..100")


def _validate_ptype(values: np.ndarray) -> np.ndarray:
    if not np.all(np.isfinite(values)):
        raise ValueError("ptype must contain only finite values")
    if not np.all(values == np.rint(values)):
        raise ValueError("ptype must contain integer category codes")
    categorical = values.astype(np.int32)
    invalid_codes = sorted(set(int(value) for value in np.unique(categorical)) - {int(code) for code in PrecipitationTypeCode})
    if invalid_codes:
        invalid = ", ".join(str(code) for code in invalid_codes)
        raise ValueError(f"ptype contains invalid code(s): {invalid}")
    return categorical


def member_diagnostic_variables(
    *,
    ptype: np.ndarray,
    hourly_precip_mm: np.ndarray,
    probabilities: dict[str, np.ndarray],
    probability_threshold_percent: float = DEFAULT_PROBABILITY_THRESHOLD_PERCENT,
    intensity_precip_threshold_mm: float = DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM,
) -> dict[str, np.ndarray]:
    expected_shape = tuple(np.asarray(ptype).shape)
    variables: dict[str, np.ndarray] = {
        "ptype": _validate_ptype(ptype),
        "hourly_precip_mm": np.asarray(hourly_precip_mm, dtype=np.float64),
    }
    _validate_same_shape("hourly_precip_mm", variables["hourly_precip_mm"], expected_shape)
    if not np.all(np.isfinite(variables["hourly_precip_mm"])):
        raise ValueError("hourly_precip_mm must contain only finite values")

    for name, _ in PROBABILITY_TYPE_FIELDS:
        probability_name = f"prob_{name}_mm"
        thresholded_name = f"precip_{name}_th_mm"
        probability = np.asarray(probabilities[probability_name], dtype=np.float64)
        _validate_same_shape(probability_name, probability, expected_shape)
        _validate_probability(probability_name, probability)
        variables[probability_name] = probability
        variables[thresholded_name] = np.where(
            (probability > probability_threshold_percent) & (variables["hourly_precip_mm"] > intensity_precip_threshold_mm),
            variables["hourly_precip_mm"],
            0.0,
        )

    return variables


def _member_variable_attrs() -> dict[str, dict[str, object]]:
    attrs: dict[str, dict[str, object]] = {
        "ptype": {"units": "code table 4.201", "long_name": "categorical precipitation type"},
        "hourly_precip_mm": {"units": "mm", "long_name": "hourly precipitation accumulation"},
    }
    for name, _ in PROBABILITY_TYPE_FIELDS:
        attrs[f"prob_{name}_mm"] = {"units": "percent", "long_name": f"microphysics-consistent probability of {name}"}
        attrs[f"precip_{name}_th_mm"] = {"units": "mm", "long_name": f"hourly precipitation where {name} probability exceeds threshold"}
    return attrs


def _final_variable_attrs(categorical_codes: tuple[PrecipitationTypeCode, ...]) -> dict[str, dict[str, object]]:
    attrs: dict[str, dict[str, object]] = {
        "valid_member_count": {"units": "count", "long_name": "valid ensemble member count"},
        "hourly_precip_mean_mm": {"units": "mm", "long_name": "ensemble mean hourly precipitation accumulation"},
    }
    for name, _ in PROBABILITY_TYPE_FIELDS:
        attrs[f"prob_{name}_mm_ens"] = {"units": "percent", "long_name": f"ensemble mean microphysics-consistent probability of {name}"}
        attrs[f"precip_{name}_th_ens"] = {"units": "mm", "long_name": f"ensemble mean thresholded hourly precipitation for {name}"}
    for code in categorical_codes:
        attrs[f"ptype_probability_{int(code)}"] = {"units": "percent", "long_name": f"categorical PTYPE ensemble frequency for code {int(code)}"}
    return attrs


def write_member_ptype_netcdf(
    path: Path,
    *,
    ptype: np.ndarray,
    attrs: dict[str, object],
) -> None:
    write_netcdf(
        path,
        {"ptype": _validate_ptype(ptype)},
        attrs={
            **attrs,
            "product": "member_ptype",
        },
        variable_attrs={"ptype": _member_variable_attrs()["ptype"]},
    )


def write_member_diagnostic_netcdf(
    path: Path,
    *,
    ptype: np.ndarray,
    hourly_precip_mm: np.ndarray,
    probabilities: dict[str, np.ndarray],
    attrs: dict[str, object],
    probability_threshold_percent: float = DEFAULT_PROBABILITY_THRESHOLD_PERCENT,
    intensity_precip_threshold_mm: float = DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM,
) -> None:
    variables = member_diagnostic_variables(
        ptype=ptype,
        hourly_precip_mm=hourly_precip_mm,
        probabilities=probabilities,
        probability_threshold_percent=probability_threshold_percent,
        intensity_precip_threshold_mm=intensity_precip_threshold_mm,
    )
    write_netcdf(
        path,
        variables,
        attrs={
            **attrs,
            "product": "member_ptype",
            "probability_scale": "percent_0_100",
            "probability_threshold_percent": probability_threshold_percent,
            "intensity_precip_threshold_mm": intensity_precip_threshold_mm,
        },
        variable_attrs=_member_variable_attrs(),
    )


def read_member_diagnostic_netcdf(path: Path) -> dict[str, np.ndarray]:
    variables = read_netcdf(path)
    missing = [name for name in MEMBER_DIAGNOSTIC_VARIABLES if name not in variables]
    if missing:
        raise ValueError(f"{path} is missing required variable(s): {', '.join(missing)}")

    expected_shape: tuple[int, ...] | None = None
    for name in MEMBER_DIAGNOSTIC_VARIABLES:
        expected_shape = _validate_same_shape(name, variables[name], expected_shape)
        if name == "ptype":
            variables[name] = _validate_ptype(variables[name])
        elif name.startswith("prob_"):
            _validate_probability(name, variables[name])
        elif not np.all(np.isfinite(variables[name])):
            raise ValueError(f"{name} must contain only finite values")
    return variables


def aggregate_member_diagnostics(
    member_diagnostics: list[dict[str, np.ndarray]],
    *,
    categorical_codes: tuple[PrecipitationTypeCode, ...] = CATEGORICAL_PROBABILITY_CODES,
) -> StepProbabilityProducts:
    if not member_diagnostics:
        raise ValueError("At least one member diagnostic is required")

    expected_shape: tuple[int, ...] | None = None
    for index, diagnostics in enumerate(member_diagnostics):
        for name in MEMBER_DIAGNOSTIC_VARIABLES:
            if name not in diagnostics:
                raise ValueError(f"Member diagnostic at index {index} is missing {name}")
            expected_shape = _validate_same_shape(name, diagnostics[name], expected_shape)
        _validate_ptype(diagnostics["ptype"])

    if expected_shape is None:
        raise ValueError("At least one member diagnostic is required")

    member_count = len(member_diagnostics)
    probability_means: dict[str, np.ndarray] = {}
    thresholded_precip_means: dict[str, np.ndarray] = {}
    for name, _ in PROBABILITY_TYPE_FIELDS:
        source_name = f"prob_{name}_mm"
        final_name = f"{source_name}_ens"
        source_stack = np.stack([np.asarray(diagnostics[source_name], dtype=np.float64) for diagnostics in member_diagnostics])
        probability_means[final_name] = np.mean(source_stack, axis=0)

        source_name = f"precip_{name}_th_mm"
        final_name = f"precip_{name}_th_ens"
        source_stack = np.stack([np.asarray(diagnostics[source_name], dtype=np.float64) for diagnostics in member_diagnostics])
        thresholded_precip_means[final_name] = np.mean(source_stack, axis=0)

    ptype_stack = np.stack([_validate_ptype(diagnostics["ptype"]) for diagnostics in member_diagnostics])
    categorical_probabilities = {
        int(code): np.mean(ptype_stack == int(code), axis=0) * 100.0
        for code in categorical_codes
    }
    hourly_precip_mean_mm = np.mean(
        np.stack([np.asarray(diagnostics["hourly_precip_mm"], dtype=np.float64) for diagnostics in member_diagnostics]),
        axis=0,
    )
    valid_member_count = np.full(expected_shape, member_count, dtype=np.float64)
    return StepProbabilityProducts(
        probability_means=probability_means,
        thresholded_precip_means=thresholded_precip_means,
        categorical_probabilities=categorical_probabilities,
        valid_member_count=valid_member_count,
        hourly_precip_mean_mm=hourly_precip_mean_mm,
    )


def _strict_preflight(
    *,
    output_root: Path,
    model: str,
    date: str,
    time_value: str,
    members: tuple[str, ...],
    processed_members: tuple[str, ...],
    failed_members: tuple[str, ...],
    start_step: int,
    max_step: int,
) -> None:
    missing_members = [member for member in members if member not in processed_members]
    if failed_members or missing_members:
        details: list[str] = []
        if failed_members:
            details.append(f"failed members: {', '.join(failed_members)}")
        if missing_members:
            details.append(f"missing processed members: {', '.join(missing_members)}")
        raise ProbabilityProductError("; ".join(details))

    missing_files: list[Path] = []
    for member in members:
        for step in range(start_step, max_step + 1):
            path = member_netcdf_path(output_root, model, date, time_value, member, step)
            if not path.exists():
                missing_files.append(path)
    if missing_files:
        preview = ", ".join(str(path) for path in missing_files[:5])
        suffix = "" if len(missing_files) <= 5 else f", ... ({len(missing_files)} total)"
        raise ProbabilityProductError(f"missing member NetCDF diagnostic files: {preview}{suffix}")


def _final_variables(step_products: StepProbabilityProducts) -> dict[str, np.ndarray]:
    variables: dict[str, np.ndarray] = {}
    variables.update(step_products.probability_means)
    variables.update(step_products.thresholded_precip_means)
    for code, values in step_products.categorical_probabilities.items():
        variables[f"ptype_probability_{code}"] = values
    variables["valid_member_count"] = step_products.valid_member_count
    variables["hourly_precip_mean_mm"] = step_products.hourly_precip_mean_mm
    return variables


def _failure_summary(
    *,
    output_dir: Path,
    members: tuple[str, ...],
    processed_members: tuple[str, ...],
    error: str,
    algorithm: str = ALGORITHM_FIRDEWSA,
) -> dict[str, object]:
    return {
        "enabled": True,
        "status": "failed",
        "format": "netcdf",
        "scale": "percent_0_100",
        "probability_threshold_percent": DEFAULT_PROBABILITY_THRESHOLD_PERCENT,
        "intensity_precip_threshold_mm": DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM,
        "products": list(probability_product_names(algorithm)),
        "diagnostic_algorithm": algorithm,
        "files_written": 0,
        "output_dir": str(output_dir),
        "required_members": list(members),
        "valid_members": list(processed_members),
        "missing_members": [member for member in members if member not in processed_members],
        "error": error,
    }


def _remove_probability_output(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _publish_probability_directory(staging_dir: Path, output_dir: Path) -> None:
    backup_dir = output_dir.parent / f".{output_dir.name}.backup.{os.getpid()}.{time.time_ns()}"
    had_existing_output = output_dir.exists() or output_dir.is_symlink()
    if had_existing_output:
        os.replace(output_dir, backup_dir)
    try:
        os.replace(staging_dir, output_dir)
    except Exception:
        if had_existing_output and backup_dir.exists():
            os.replace(backup_dir, output_dir)
        raise
    if had_existing_output:
        shutil.rmtree(backup_dir, ignore_errors=True)


def disabled_probability_summary(
    output_root: Path,
    model: str,
    date: str,
    time_value: str,
    members: tuple[str, ...],
    *,
    algorithm: str = ALGORITHM_FIRDEWSA,
) -> dict[str, object]:
    return {
        "enabled": False,
        "status": "skipped",
        "format": "netcdf",
        "scale": "percent_0_100",
        "probability_threshold_percent": DEFAULT_PROBABILITY_THRESHOLD_PERCENT,
        "intensity_precip_threshold_mm": DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM,
        "products": list(probability_product_names(algorithm)),
        "diagnostic_algorithm": algorithm,
        "files_written": 0,
        "output_dir": str(probability_output_dir(output_root, model, date, time_value)),
        "required_members": list(members),
        "valid_members": [],
        "missing_members": [],
    }


def generate_probability_products(
    *,
    output_root: Path,
    model: str,
    date: str,
    time_value: str,
    members: tuple[str, ...],
    processed_members: tuple[str, ...],
    failed_members: tuple[str, ...],
    start_step: int,
    max_step: int,
    algorithm: str = ALGORITHM_FIRDEWSA,
) -> dict[str, object]:
    categorical_codes = _categorical_codes(algorithm)
    output_dir = probability_output_dir(output_root, model, date, time_value)
    start = time.perf_counter()
    preflight_s = 0.0
    read_s = 0.0
    aggregate_s = 0.0
    write_s = 0.0
    publish_s = 0.0

    try:
        preflight_start = time.perf_counter()
        _strict_preflight(
            output_root=output_root,
            model=model,
            date=date,
            time_value=time_value,
            members=members,
            processed_members=processed_members,
            failed_members=failed_members,
            start_step=start_step,
            max_step=max_step,
        )
        preflight_s += time.perf_counter() - preflight_start
    except ProbabilityProductError as exc:
        _remove_probability_output(output_dir)
        return _failure_summary(
            output_dir=output_dir,
            members=members,
            processed_members=processed_members,
            error=str(exc),
            algorithm=algorithm,
        )

    run_dir = output_dir.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(dir=run_dir, prefix=".probabilities."))
    staged_files: list[Path] = []
    try:
        for step in range(start_step, max_step + 1):
            read_start = time.perf_counter()
            diagnostics = [
                read_member_diagnostic_netcdf(member_netcdf_path(output_root, model, date, time_value, member, step))
                for member in members
            ]
            read_s += time.perf_counter() - read_start
            aggregate_start = time.perf_counter()
            step_products = aggregate_member_diagnostics(diagnostics, categorical_codes=categorical_codes)
            aggregate_s += time.perf_counter() - aggregate_start

            staged_path = staging_dir / f"lfff{step_token(step)}.ptype_prob.nc"
            write_start = time.perf_counter()
            write_netcdf(
                staged_path,
                _final_variables(step_products),
                attrs={
                    "product": "ensemble_ptype_probabilities",
                    "model": model,
                    "date": date,
                    "time": time_value,
                    "step": step,
                    "probability_scale": "percent_0_100",
                    "probability_threshold_percent": DEFAULT_PROBABILITY_THRESHOLD_PERCENT,
                    "intensity_precip_threshold_mm": DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM,
                    "required_member_count": len(members),
                    "diagnostic_algorithm": algorithm,
                },
                variable_attrs=_final_variable_attrs(categorical_codes),
            )
            write_s += time.perf_counter() - write_start
            staged_files.append(staged_path)

        publish_start = time.perf_counter()
        _publish_probability_directory(staging_dir, output_dir)
        publish_s += time.perf_counter() - publish_start
    except Exception as exc:
        _remove_probability_output(output_dir)
        return _failure_summary(
            output_dir=output_dir,
            members=members,
            processed_members=processed_members,
            error=f"{type(exc).__name__}: {exc}",
            algorithm=algorithm,
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return {
        "enabled": True,
        "status": "ok",
        "format": "netcdf",
        "scale": "percent_0_100",
        "probability_threshold_percent": DEFAULT_PROBABILITY_THRESHOLD_PERCENT,
        "intensity_precip_threshold_mm": DEFAULT_INTENSITY_PRECIP_THRESHOLD_MM,
        "products": list(probability_product_names(algorithm)),
        "diagnostic_algorithm": algorithm,
        "files_written": len(staged_files),
        "output_dir": str(output_dir),
        "required_members": list(members),
        "valid_members": list(processed_members),
        "missing_members": [],
        "start_step": start_step,
        "max_step": max_step,
        "timings_s": {
            "preflight_s": round(preflight_s, 3),
            "read_s": round(read_s, 3),
            "aggregate_s": round(aggregate_s, 3),
            "write_s": round(write_s, 3),
            "publish_s": round(publish_s, 3),
            "wall_s": round(time.perf_counter() - start, 3),
        },
    }
