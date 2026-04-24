"""Operational batch runner for the precipitation-type diagnostic."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import time

import numpy as np
from earthkit.data import from_source

from .constants import DEFAULT_VERTICAL_CUTOFF_M, PRECIPITATION_TYPE_NAMES, PrecipitationTypeCode
from .gribio import (
    _scan_grib_file_fast,
    _previous_step,
    available_members,
    available_steps,
    bootstrap_eccodes_definitions,
    derive_vertical_level_selection,
    validate_precip_mask_threshold_mm,
    write_output_grib,
)
from .grid import GridInputs, diagnose_grid_categorical


DEFAULT_INPUT_ROOT = Path(os.environ.get("PRECIP_TYPE_DIAG_INPUT_ROOT", "/opr/osm/inn/cache"))
DEFAULT_OUTPUT_ROOT = Path(os.environ.get("PRECIP_TYPE_DIAG_OUTPUT_ROOT", "/tmp/precip_type_diag"))


@dataclass(frozen=True)
class OperationalConfig:
    model: str
    members: tuple[str, ...]
    input_root: Path
    output_root: Path
    precip_mask_threshold_mm: float
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M
    max_workers: int = 1


MODEL_CONFIGS = {
    "ICON-CH1-EPS": OperationalConfig(
        model="ICON-CH1-EPS",
        members=tuple(f"{member:03d}" for member in range(11)),
        input_root=DEFAULT_INPUT_ROOT,
        output_root=DEFAULT_OUTPUT_ROOT,
        precip_mask_threshold_mm=0.0,
        vertical_cutoff_m=DEFAULT_VERTICAL_CUTOFF_M,
        max_workers=4,
    ),
    "ICON-CH2-EPS": OperationalConfig(
        model="ICON-CH2-EPS",
        members=tuple(f"{member:03d}" for member in range(21)),
        input_root=DEFAULT_INPUT_ROOT,
        output_root=DEFAULT_OUTPUT_ROOT,
        precip_mask_threshold_mm=0.0,
        vertical_cutoff_m=DEFAULT_VERTICAL_CUTOFF_M,
        max_workers=6,
    ),
}


def config_for_model(
    model: str,
    *,
    input_root: Path | None = None,
    output_root: Path | None = None,
    precip_mask_threshold_mm: float | None = None,
) -> OperationalConfig:
    base = MODEL_CONFIGS[model]
    threshold = (
        validate_precip_mask_threshold_mm(precip_mask_threshold_mm)
        if precip_mask_threshold_mm is not None
        else base.precip_mask_threshold_mm
    )
    return OperationalConfig(
        model=model,
        members=base.members,
        input_root=Path(input_root) if input_root is not None else base.input_root,
        output_root=Path(output_root) if output_root is not None else base.output_root,
        precip_mask_threshold_mm=threshold,
        vertical_cutoff_m=base.vertical_cutoff_m,
        max_workers=base.max_workers,
    )


def resolve_run_id(model: str, input_root: Path, run: str) -> str:
    fcst_ring = Path(input_root) / model / "FCST_RING"
    if run != "latest":
        run_dir = fcst_ring / run / "icon"
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Missing input run directory: {run_dir}")
        return run

    candidates = sorted(
        path.name
        for path in fcst_ring.iterdir()
        if path.is_dir() and (path / "icon").is_dir()
    )
    if not candidates:
        raise FileNotFoundError(f"No runs found under {fcst_ring}")
    return candidates[-1]


def input_run_dir(config: OperationalConfig, run_id: str) -> Path:
    return config.input_root / config.model / "FCST_RING" / run_id / "icon"


def output_run_dir(config: OperationalConfig, run_id: str) -> Path:
    return config.output_root / config.model / run_id


def operational_output_path(base_output_dir: Path, member: str, step: str) -> Path:
    return Path(base_output_dir) / member / f"lfff{step}.ptype.grib2"


def _atomic_write_json(payload: dict[str, object], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f"{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_path, destination)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
    return destination


def _is_valid_output(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    bootstrap_eccodes_definitions()
    try:
        fieldset = from_source("file", str(path))
        if len(fieldset) != 1:
            return False
        field = fieldset[0]
        return (
            field.metadata("shortName") == "PTYPE"
            and field.metadata("parameterCategory") == 1
            and field.metadata("parameterNumber") == 19
        )
    except Exception:
        return False


def _category_counts(categorical_codes: np.ndarray) -> dict[str, int]:
    values, counts = np.unique(np.asarray(categorical_codes, dtype=int), return_counts=True)
    result = {name: 0 for name in PRECIPITATION_TYPE_NAMES.values()}
    for value, count in zip(values, counts):
        try:
            code = PrecipitationTypeCode(int(value))
        except ValueError:
            continue
        result[PRECIPITATION_TYPE_NAMES[code]] = int(count)
    return result


def _merge_category_counts(items: list[dict[str, int]]) -> dict[str, int]:
    merged: dict[str, int] = {name: 0 for name in PRECIPITATION_TYPE_NAMES.values()}
    for item in items:
        for key, value in item.items():
            merged[key] = merged.get(key, 0) + int(value)
    return merged


def _member_summary_template(member: str) -> dict[str, object]:
    return {
        "member": member,
        "written": [],
        "skipped": [],
        "failed": [],
        "category_counts": {name: 0 for name in PRECIPITATION_TYPE_NAMES.values()},
    }


def process_member_run(
    *,
    member_dir: Path,
    member: str,
    output_dir: Path,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
    overwrite: bool,
) -> dict[str, object]:
    start = time.perf_counter()
    bootstrap_eccodes_definitions()
    threshold = validate_precip_mask_threshold_mm(precip_mask_threshold_mm)

    summary = _member_summary_template(member)
    constants_file = member_dir / "lfff00000000c"
    if not constants_file.exists():
        summary["failed"].append({"member": member, "reason": "missing constants file"})
        summary["runtime_s"] = round(time.perf_counter() - start, 3)
        return summary

    constants_fields, _ = _scan_grib_file_fast(constants_file, ("HHL",))
    full_half_level_height_m = constants_fields["HHL"]
    selection = derive_vertical_level_selection(full_half_level_height_m, vertical_cutoff_m)
    half_level_height_m = full_half_level_height_m[selection.half_level_start :]

    steps = available_steps(member_dir)
    previous_step: str | None = None
    previous_total_precip: np.ndarray | None = None

    for step in steps:
        current_file = member_dir / f"lfff{step}"
        current_fields, template_field = _scan_grib_file_fast(
            current_file,
            ("T", "P", "QV", "TOT_PREC", "T_G"),
            level_start_by_name={
                "T": selection.full_level_start,
                "P": selection.full_level_start,
                "QV": selection.full_level_start,
            },
            capture_template_for="TOT_PREC",
        )
        total_precip_current = current_fields["TOT_PREC"]

        expected_previous = _previous_step(step)
        processable = expected_previous is None
        total_precip_mm = total_precip_current
        if expected_previous is not None:
            processable = previous_step == expected_previous and previous_total_precip is not None
            if processable:
                total_precip_mm = total_precip_current - previous_total_precip

        if not processable:
            summary["skipped"].append(
                {
                    "step": step,
                    "reason": "missing previous forecast file",
                }
            )
            previous_step = step
            previous_total_precip = total_precip_current
            continue

        destination = operational_output_path(output_dir, member, step)
        if not overwrite and _is_valid_output(destination):
            summary["skipped"].append({"step": step, "reason": "existing valid output"})
            previous_step = step
            previous_total_precip = total_precip_current
            continue

        try:
            categorical = diagnose_grid_categorical(
                GridInputs(
                    temperature_k=current_fields["T"],
                    pressure_pa=current_fields["P"],
                    specific_humidity=current_fields["QV"],
                    half_level_height_m=half_level_height_m,
                    total_precip_mm=total_precip_mm,
                    ground_temperature_c=current_fields["T_G"] - 273.15,
                ),
                precip_mask_threshold_mm=threshold,
            )
            write_output_grib(template_field, categorical, destination)
            summary["written"].append({"step": step, "path": str(destination)})
            counts = _category_counts(categorical)
            summary["category_counts"] = _merge_category_counts([summary["category_counts"], counts])
        except Exception as exc:
            summary["failed"].append({"step": step, "reason": f"{type(exc).__name__}: {exc}"})

        previous_step = step
        previous_total_precip = total_precip_current

    summary["runtime_s"] = round(time.perf_counter() - start, 3)
    return summary


def _process_member_worker(
    *,
    input_run: Path,
    output_dir: Path,
    member: str,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float,
    overwrite: bool,
) -> dict[str, object]:
    return process_member_run(
        member_dir=input_run / member,
        member=member,
        output_dir=output_dir,
        precip_mask_threshold_mm=precip_mask_threshold_mm,
        vertical_cutoff_m=vertical_cutoff_m,
        overwrite=overwrite,
    )


def run_operational(
    *,
    model: str,
    run: str,
    input_root: Path | None = None,
    output_root: Path | None = None,
    precip_mask_threshold_mm: float | None = None,
    overwrite: bool = False,
    summary_json: Path | None = None,
) -> dict[str, object]:
    config = config_for_model(
        model,
        input_root=input_root,
        output_root=output_root,
        precip_mask_threshold_mm=precip_mask_threshold_mm,
    )
    run_id = resolve_run_id(model, config.input_root, run)
    input_run = input_run_dir(config, run_id)
    output_dir = output_run_dir(config, run_id)
    members = [member for member in config.members if (input_run / member).is_dir()]

    start = time.perf_counter()
    member_results: dict[str, dict[str, object]] = {}
    if config.max_workers <= 1 or len(members) <= 1:
        for member in members:
            member_results[member] = _process_member_worker(
                input_run=input_run,
                output_dir=output_dir,
                member=member,
                precip_mask_threshold_mm=config.precip_mask_threshold_mm,
                vertical_cutoff_m=config.vertical_cutoff_m,
                overwrite=overwrite,
            )
    else:
        with ProcessPoolExecutor(max_workers=min(config.max_workers, len(members))) as executor:
            futures = {
                executor.submit(
                    _process_member_worker,
                    input_run=input_run,
                    output_dir=output_dir,
                    member=member,
                    precip_mask_threshold_mm=config.precip_mask_threshold_mm,
                    vertical_cutoff_m=config.vertical_cutoff_m,
                    overwrite=overwrite,
                ): member
                for member in members
            }
            for future in as_completed(futures):
                member = futures[future]
                try:
                    member_results[member] = future.result()
                except Exception as exc:
                    member_results[member] = {
                        "member": member,
                        "written": [],
                        "skipped": [],
                        "failed": [{"member": member, "reason": f"{type(exc).__name__}: {exc}"}],
                        "category_counts": {name: 0 for name in PRECIPITATION_TYPE_NAMES.values()},
                        "runtime_s": 0.0,
                    }

    total_runtime_s = round(time.perf_counter() - start, 3)
    ordered_results = {member: member_results[member] for member in members}
    summary = {
        "model": model,
        "run": run_id,
        "input_run": str(input_run),
        "output_dir": str(output_dir),
        "precip_mask_threshold_mm": config.precip_mask_threshold_mm,
        "vertical_cutoff_m": config.vertical_cutoff_m,
        "diagnostic_backend": "numba",
        "io_backend": "fast",
        "max_workers": min(config.max_workers, max(1, len(members))),
        "members": members,
        "per_member": ordered_results,
        "written_count": sum(len(item["written"]) for item in ordered_results.values()),
        "skipped_count": sum(len(item["skipped"]) for item in ordered_results.values()),
        "failed_count": sum(len(item["failed"]) for item in ordered_results.values()),
        "category_counts": _merge_category_counts([item["category_counts"] for item in ordered_results.values()]),
        "runtime_s": total_runtime_s,
    }

    summary_path = output_dir / "summary.json"
    _atomic_write_json(summary, summary_path)
    if summary_json is not None:
        _atomic_write_json(summary, Path(summary_json))
    return summary
