"""Extract FDB column profiles for scientific acceptance evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .constants import DEFAULT_VERTICAL_CUTOFF_M, PRECIPITATION_TYPE_NAMES, PrecipitationTypeCode
from .gribio import derive_vertical_level_selection
from .grid import GridInputs, diagnose_grid_categorical_with_quality
from .operational import (
    MODEL_TO_FDB,
    Timings,
    _atomic_write_json,
    _fetch_chunk,
    _fetch_hhl,
    _make_run,
    _stack_level_fields,
    _validate_complete_run,
)
from .profile import ColumnProfile, diagnose_column


@dataclass(frozen=True)
class ProfileSamplePoint:
    name: str
    flat_index: int | None = None
    y: int | None = None
    x: int | None = None
    expected: PrecipitationTypeCode | None = None
    metadata: dict[str, object] | None = None


def _parse_code(value: str | int) -> PrecipitationTypeCode:
    if isinstance(value, int):
        return PrecipitationTypeCode(value)
    text = str(value).strip()
    if text.isdigit():
        return PrecipitationTypeCode(int(text))
    normalized = text.lower()
    for code, name in PRECIPITATION_TYPE_NAMES.items():
        if normalized == name:
            return code
    raise ValueError(f"Unknown precipitation type code: {value}")


def _parse_steps(value: str) -> list[int]:
    text = value.strip()
    if "/to/" in text:
        start_text, remainder = text.split("/to/", 1)
        if "/by/" in remainder:
            stop_text, by_text = remainder.split("/by/", 1)
            stride = int(by_text)
        else:
            stop_text = remainder
            stride = 1
        if stride <= 0:
            raise ValueError(f"Step stride must be positive, got {stride}")
        return list(range(int(start_text), int(stop_text) + 1, stride))
    steps = [int(item.strip()) for item in text.split(",") if item.strip()]
    if not steps:
        raise ValueError("At least one step is required")
    if any(step < 0 for step in steps):
        raise ValueError(f"Steps must be non-negative, got {steps}")
    return sorted(set(steps))


def load_profile_sample_points(path: Path) -> list[ProfileSamplePoint]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    points: list[ProfileSamplePoint] = []
    for entry in payload["points"]:
        expected = entry.get("expected")
        points.append(
            ProfileSamplePoint(
                name=str(entry["name"]),
                flat_index=int(entry["flat_index"]) if "flat_index" in entry else None,
                y=int(entry["y"]) if "y" in entry else None,
                x=int(entry["x"]) if "x" in entry else None,
                expected=_parse_code(expected) if expected is not None else None,
                metadata=dict(entry.get("metadata", {})),
            )
        )
    if not points:
        raise ValueError(f"Profile sample point file {path} does not contain any points")
    return points


def _point_flat_index(point: ProfileSamplePoint, horizontal_shape: tuple[int, ...]) -> int:
    npoints = int(np.prod(horizontal_shape))
    if point.flat_index is not None:
        if point.flat_index < 0 or point.flat_index >= npoints:
            raise ValueError(f"Point {point.name!r} flat_index {point.flat_index} is outside 0..{npoints - 1}")
        return point.flat_index
    if point.y is not None and point.x is not None:
        if len(horizontal_shape) != 2:
            raise ValueError("Point y/x addressing requires a two-dimensional horizontal grid")
        if point.y < 0 or point.y >= horizontal_shape[0] or point.x < 0 or point.x >= horizontal_shape[1]:
            raise ValueError(f"Point {point.name!r} y/x {(point.y, point.x)} is outside shape {horizontal_shape}")
        return int(np.ravel_multi_index((point.y, point.x), horizontal_shape))
    raise ValueError(f"Point {point.name!r} must define either flat_index or both y and x")


def _auto_select_points(
    *,
    categorical: np.ndarray,
    total_precip_mm: np.ndarray,
    step: int,
    codes: tuple[PrecipitationTypeCode, ...],
    samples_per_type: int,
) -> list[ProfileSamplePoint]:
    flat_codes = categorical.reshape(-1)
    flat_precip = total_precip_mm.reshape(-1)
    points: list[ProfileSamplePoint] = []
    for code in codes:
        indices = np.flatnonzero(flat_codes == int(code))
        if indices.size == 0:
            continue
        ordered = indices[np.argsort(flat_precip[indices])[::-1]]
        for rank, flat_index in enumerate(ordered[:samples_per_type], start=1):
            name = f"diagnostic_{PRECIPITATION_TYPE_NAMES[code]}_step{step:03d}_rank{rank:02d}"
            points.append(
                ProfileSamplePoint(
                    name=name,
                    flat_index=int(flat_index),
                    metadata={
                        "selection": "diagnostic_category_candidate",
                        "selection_code": int(code),
                        "selection_name": PRECIPITATION_TYPE_NAMES[code],
                        "rank_within_step_and_category": rank,
                    },
                )
            )
    return points


def _case_from_point(
    *,
    point: ProfileSamplePoint,
    step: int,
    flat_index: int,
    horizontal_shape: tuple[int, ...],
    temperature_k: np.ndarray,
    pressure_pa: np.ndarray,
    specific_humidity: np.ndarray,
    full_level_height_m: np.ndarray,
    total_precip_mm: np.ndarray,
    ground_temperature_c: np.ndarray,
    metadata: dict[str, object],
) -> dict[str, object]:
    column = ColumnProfile(
        temperature_k=temperature_k[:, flat_index],
        pressure_pa=pressure_pa[:, flat_index],
        specific_humidity=specific_humidity[:, flat_index],
        full_level_height_m=full_level_height_m[:, flat_index],
        total_precip_mm=float(total_precip_mm.reshape(-1)[flat_index]),
        ground_temperature_c=float(ground_temperature_c.reshape(-1)[flat_index]),
    )
    diagnostic = diagnose_column(column)
    case_metadata: dict[str, object] = {
        **metadata,
        **(point.metadata or {}),
        "point_name": point.name,
        "step": step,
        "flat_index": flat_index,
        "horizontal_shape": list(horizontal_shape),
        "diagnostic_code": int(diagnostic.categorical_code),
        "diagnostic_name": PRECIPITATION_TYPE_NAMES[diagnostic.categorical_code],
    }
    if point.y is not None and point.x is not None:
        case_metadata["y"] = point.y
        case_metadata["x"] = point.x

    case: dict[str, object] = {
        "name": f"{point.name}_step{step:03d}",
        "temperature_k": column.temperature_k.tolist(),
        "pressure_pa": column.pressure_pa.tolist(),
        "specific_humidity": column.specific_humidity.tolist(),
        "full_level_height_m": column.full_level_height_m.tolist(),
        "total_precip_mm": column.total_precip_mm,
        "ground_temperature_c": column.ground_temperature_c,
        "metadata": case_metadata,
    }
    if point.expected is not None:
        case["expected"] = PRECIPITATION_TYPE_NAMES[point.expected]
    return case


def extract_profile_samples(
    *,
    model: str,
    member: str,
    date: str,
    time_value: str,
    steps: list[int],
    points: list[ProfileSamplePoint] | None = None,
    auto_select_codes: tuple[PrecipitationTypeCode, ...] = (),
    samples_per_type: int = 1,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
    validate_inputs: bool = True,
) -> dict[str, object]:
    if model not in MODEL_TO_FDB:
        raise ValueError(f"Unsupported model {model!r}")
    if not steps:
        raise ValueError("At least one step is required")
    if samples_per_type <= 0:
        raise ValueError(f"samples_per_type must be positive, got {samples_per_type}")
    if not points and not auto_select_codes:
        raise ValueError("Provide explicit points or auto_select_codes")

    max_step = max(steps)
    if validate_inputs:
        _validate_complete_run(model, member, date, time_value, max_step)

    timings = Timings()
    run = _make_run(model, member, date, time_value, max_step)
    hhl = _fetch_hhl(run, timings)
    selection = derive_vertical_level_selection(hhl, vertical_cutoff_m)
    half_level_height_m = hhl[selection.half_level_start :]
    half_flat = half_level_height_m.reshape(half_level_height_m.shape[0], -1)
    full_level_height_m = 0.5 * (half_flat[:-1] + half_flat[1:])

    first_step = 0 if 0 in steps else min(steps) - 1
    fetched_steps = list(range(first_step, max_step + 1))
    chunk = _fetch_chunk(run, steps=fetched_steps, full_level_start=selection.full_level_start, timings=timings)

    cases: list[dict[str, object]] = []
    for step in steps:
        step_ml = chunk.ml_by_step[step]
        temperature_k = _stack_level_fields(step_ml["T"], selection.retained_full_levels)
        pressure_pa = _stack_level_fields(step_ml["P"], selection.retained_full_levels)
        specific_humidity = _stack_level_fields(step_ml["QV"], selection.retained_full_levels)
        total_precip_current = chunk.total_precip_by_step[step].to_numpy(flatten=False)
        if step == 0:
            total_precip_mm = total_precip_current
        else:
            total_precip_mm = total_precip_current - chunk.total_precip_by_step[step - 1].to_numpy(flatten=False)
        ground_temperature_c = chunk.ground_temperature_by_step[step].to_numpy(flatten=False) - 273.15
        horizontal_shape = tuple(total_precip_mm.shape)

        step_points = list(points or [])
        if auto_select_codes:
            categorical = diagnose_grid_categorical_with_quality(
                GridInputs(
                    temperature_k=temperature_k,
                    pressure_pa=pressure_pa,
                    specific_humidity=specific_humidity,
                    half_level_height_m=half_level_height_m,
                    total_precip_mm=total_precip_mm,
                    ground_temperature_c=ground_temperature_c,
                )
            ).categorical
            step_points.extend(
                _auto_select_points(
                    categorical=categorical,
                    total_precip_mm=total_precip_mm,
                    step=step,
                    codes=auto_select_codes,
                    samples_per_type=samples_per_type,
                )
            )

        metadata = {
            "source": "realtime_fdb",
            "model": model,
            "fdb_model": MODEL_TO_FDB[model],
            "date": date,
            "time": time_value,
            "member": member,
            "vertical_cutoff_m": float(vertical_cutoff_m),
            "retained_full_levels": selection.retained_full_levels,
        }
        for point in step_points:
            flat_index = _point_flat_index(point, horizontal_shape)
            cases.append(
                _case_from_point(
                    point=point,
                    step=step,
                    flat_index=flat_index,
                    horizontal_shape=horizontal_shape,
                    temperature_k=temperature_k.reshape(temperature_k.shape[0], -1),
                    pressure_pa=pressure_pa.reshape(pressure_pa.shape[0], -1),
                    specific_humidity=specific_humidity.reshape(specific_humidity.shape[0], -1),
                    full_level_height_m=full_level_height_m,
                    total_precip_mm=total_precip_mm,
                    ground_temperature_c=ground_temperature_c,
                    metadata=metadata,
                )
            )

    return {
        "cases": cases,
        "metadata": {
            "model": model,
            "date": date,
            "time": time_value,
            "member": member,
            "steps": steps,
            "timings_s": timings.as_dict(),
            "note": "Unlabeled diagnostic candidates need independent observation labels before scientific acceptance.",
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract ICON FDB column profiles for validation evidence")
    parser.add_argument("--model", choices=sorted(MODEL_TO_FDB), required=True)
    parser.add_argument("--member", default="000")
    parser.add_argument("--date", required=True, help="FDB run date YYYYMMDD")
    parser.add_argument("--time", dest="time_value", required=True, help="FDB run time HHMM")
    parser.add_argument("--steps", required=True, help="Comma list like 0,1,2 or range like 0/to/3/by/1")
    parser.add_argument("--points-json", type=Path, default=None)
    parser.add_argument("--select-diagnostic-types", default="", help="Comma list of category names/codes to auto-select")
    parser.add_argument("--samples-per-type", type=int, default=1)
    parser.add_argument("--vertical-cutoff-m", type=float, default=DEFAULT_VERTICAL_CUTOFF_M)
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    points = load_profile_sample_points(args.points_json) if args.points_json is not None else None
    auto_select_codes = tuple(
        _parse_code(item.strip()) for item in args.select_diagnostic_types.split(",") if item.strip()
    )
    payload = extract_profile_samples(
        model=args.model,
        member=args.member,
        date=args.date,
        time_value=args.time_value,
        steps=_parse_steps(args.steps),
        points=points,
        auto_select_codes=auto_select_codes,
        samples_per_type=args.samples_per_type,
        vertical_cutoff_m=args.vertical_cutoff_m,
        validate_inputs=not args.skip_validation,
    )
    _atomic_write_json(payload, args.output)
    print(json.dumps(payload["metadata"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
