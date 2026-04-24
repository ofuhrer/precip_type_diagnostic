"""GRIB discovery, loading, and writing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import re
from datetime import timedelta
import tempfile
import time
from typing import Iterable

import eccodes
import numpy as np
from earthkit.data import from_source
from earthkit.data.encoders.grib import GribEncoder

from .constants import (
    DEFAULT_VERTICAL_CUTOFF_M,
    INPUT_PARAM_IDS,
    OUTPUT_PARAM_ID,
    OUTPUT_SHORT_NAME,
    REQUIRED_INPUT_FIELDS,
)
from .grid import GridInputs, diagnose_grid_categorical

FORECAST_FILE_RE = re.compile(r"^lfff(\d{8})$")
THREE_D_FIELDS = frozenset({"T", "P", "QV", "HHL"})
GRIB_INDEX_CACHE_ENV = "PRECIP_TYPE_DIAG_GRIB_INDEX_CACHE"
GRIB_INDEX_CACHE_DISABLED = frozenset({"0", "false", "no", "off", "none", ""})
GRIB_INDEX_CACHE_VERSION = "v2"
GRIB_INDEX_MAX_AGE_DAYS = 10


class MissingFieldError(RuntimeError):
    """Required GRIB field is missing."""


class MissingFileError(RuntimeError):
    """Required GRIB file is missing."""


@dataclass(frozen=True)
class MemberHourJob:
    member: str
    step: str
    current_file: Path
    previous_file: Path | None
    constants_file: Path


@dataclass(frozen=True)
class GribTemplateMessage:
    message_bytes: bytes
    values_shape: tuple[int, ...]


@dataclass(frozen=True)
class LegacyTemplateField:
    field: object


@dataclass(frozen=True)
class VerticalLevelSelection:
    full_level_start: int
    half_level_start: int
    retained_full_levels: int


def _package_definitions_dir() -> Path:
    return Path(__file__).resolve().parent / "definitions"


def _grib_index_cache_dir() -> Path | None:
    configured = os.environ.get(GRIB_INDEX_CACHE_ENV)
    if configured is not None and configured.strip().lower() in GRIB_INDEX_CACHE_DISABLED:
        return None
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "precip_type_diag_grib_indexes"


def _grib_index_cache_path(path: Path) -> Path | None:
    cache_dir = _grib_index_cache_dir()
    if cache_dir is None:
        return None

    source = Path(path).resolve()
    stat = source.stat()
    definitions_path = os.environ.get("ECCODES_DEFINITION_PATH", eccodes.codes_definition_path())
    fingerprint = "|".join(
        (
            GRIB_INDEX_CACHE_VERSION,
            str(source),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            definitions_path,
            eccodes.codes_get_api_version(),
        )
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.idx"


def _prune_grib_index_cache(cache_dir: Path, *, max_age_days: int = GRIB_INDEX_MAX_AGE_DAYS) -> None:
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    try:
        candidates = list(cache_dir.glob("*.idx"))
    except OSError:
        return
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink()
        except OSError:
            continue


def _open_param_id_index(path: Path):
    source = Path(path).resolve()
    cache_path = _grib_index_cache_path(path)
    if cache_path is None:
        return eccodes.codes_index_new_from_file(str(source), ["paramId:l"])

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    _prune_grib_index_cache(cache_path.parent)
    if cache_path.exists():
        return eccodes.codes_index_read(str(cache_path))

    temp_path: Path | None = None
    index = eccodes.codes_index_new_from_file(str(source), ["paramId:l"])
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_path.parent,
            prefix=f"{cache_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
        eccodes.codes_index_write(index, str(temp_path))
        os.replace(temp_path, cache_path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
    finally:
        eccodes.codes_index_release(index)

    return eccodes.codes_index_read(str(cache_path))


def _candidate_meteoswiss_definition_dirs() -> list[Path]:
    candidates: list[Path] = []
    env_dir = os.environ.get("PRECIP_TYPE_DIAG_COSMO_DEFS")
    if env_dir:
        candidates.append(Path(env_dir))

    try:
        import eccodes_cosmo_resources  # type: ignore

        get_path = getattr(eccodes_cosmo_resources, "get_definitions_path", None)
        if callable(get_path):
            candidates.append(Path(get_path()))
        module_dir = Path(eccodes_cosmo_resources.__file__).resolve().parent
        candidates.extend(sorted(module_dir.glob("definitions.*"), reverse=True))
    except Exception:
        pass

    candidates.extend(
        [
            Path("/tmp/eccodes-cosmo-resources-python/definitions.edzw-2.38.3-1"),
            Path("/tmp/eccodes-cosmo-defs/definitions.edzw-2.38.3-1"),
        ]
    )
    return [candidate for candidate in candidates if candidate.exists()]


def bootstrap_eccodes_definitions() -> str:
    """Prepend MeteoSwiss definitions and the project-local overlay to ecCodes."""

    current = eccodes.codes_definition_path()
    local_defs = _package_definitions_dir()
    meteoswiss_defs = _candidate_meteoswiss_definition_dirs()

    if not meteoswiss_defs:
        raise RuntimeError(
            "Could not locate MeteoSwiss ecCodes definitions. "
            "Install eccodes-cosmo-resources-python or set PRECIP_TYPE_DIAG_COSMO_DEFS."
        )

    preferred = [str(local_defs), str(meteoswiss_defs[0])]
    current_parts = [part for part in current.split(":") if part]
    combined_parts: list[str] = []
    for part in [*preferred, *current_parts]:
        if part not in combined_parts:
            combined_parts.append(part)
    combined = ":".join(combined_parts)
    eccodes.codes_set_definitions_path(combined)
    os.environ["ECCODES_DEFINITION_PATH"] = combined
    return combined


def parse_members(value: str) -> list[str] | str:
    if value == "all":
        return value
    members = [item.strip() for item in value.split(",") if item.strip()]
    if not members:
        raise ValueError("Expected 'all' or at least one member like 000")
    invalid = [member for member in members if not re.fullmatch(r"\d{3}", member)]
    if invalid:
        raise ValueError(f"Invalid member identifier(s): {', '.join(invalid)}")
    return members


def parse_hours(value: str) -> list[str] | str:
    if value == "all":
        return value
    hours = [item.strip() for item in value.split(",") if item.strip()]
    if not hours:
        raise ValueError("Expected 'all' or at least one step like 00010000")
    invalid = [hour for hour in hours if not re.fullmatch(r"\d{8}", hour)]
    if invalid:
        raise ValueError(f"Invalid ICON step string(s): {', '.join(invalid)}")
    return hours


def validate_precip_mask_threshold_mm(value: float) -> float:
    threshold = float(value)
    if not np.isfinite(threshold):
        raise ValueError(f"precip_mask_threshold_mm must be finite, got {value!r}")
    if threshold < 0.0:
        raise ValueError(f"precip_mask_threshold_mm must be non-negative, got {value!r}")
    return threshold


def available_members(input_run: Path) -> list[str]:
    return sorted(path.name for path in input_run.iterdir() if path.is_dir() and re.fullmatch(r"\d{3}", path.name))


def available_steps(member_dir: Path) -> list[str]:
    return sorted(match.group(1) for path in member_dir.iterdir() if (match := FORECAST_FILE_RE.match(path.name)))


def _parse_step(step: str) -> timedelta:
    if not re.fullmatch(r"\d{8}", step):
        raise ValueError(f"Invalid ICON step string: {step}")
    days = int(step[0:2])
    hours = int(step[2:4])
    minutes = int(step[4:6])
    seconds = int(step[6:8])
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _format_step(offset: timedelta) -> str:
    total_seconds = int(offset.total_seconds())
    days, remainder = divmod(total_seconds, 24 * 3600)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days:02d}{hours:02d}{minutes:02d}{seconds:02d}"


def _previous_step(step: str) -> str | None:
    offset = _parse_step(step)
    if offset <= timedelta(0):
        return None
    previous = offset - timedelta(hours=1)
    if previous < timedelta(0):
        return None
    return _format_step(previous)


def build_jobs(input_run: Path, members: list[str] | str, hours: list[str] | str) -> tuple[list[MemberHourJob], list[str]]:
    input_run = Path(input_run)
    selected_members = available_members(input_run) if members == "all" else members

    jobs: list[MemberHourJob] = []
    skipped: list[str] = []
    for member in selected_members:
        member_dir = input_run / member
        if not member_dir.is_dir():
            skipped.append(f"{member}: missing member directory")
            continue

        selected_hours = available_steps(member_dir) if hours == "all" else hours
        constants_file = member_dir / "lfff00000000c"
        if not constants_file.exists():
            if hours == "all":
                skipped.append(f"{member}: missing constants file")
                continue
            raise MissingFileError(f"Missing constants file: {constants_file}")

        for step in selected_hours:
            current_file = member_dir / f"lfff{step}"
            if not current_file.exists():
                if hours == "all" or members == "all":
                    skipped.append(f"{member}/{step}: missing forecast file")
                    continue
                raise MissingFileError(f"Missing forecast file: {current_file}")

            previous_step = _previous_step(step)
            previous_file = member_dir / f"lfff{previous_step}" if previous_step is not None else None
            if previous_file is not None and not previous_file.exists():
                if hours == "all" or members == "all":
                    skipped.append(f"{member}/{step}: missing previous forecast file")
                    continue
                raise MissingFileError(f"Missing previous forecast file: {previous_file}")

            jobs.append(
                MemberHourJob(
                    member=member,
                    step=step,
                    current_file=current_file,
                    previous_file=previous_file,
                    constants_file=constants_file,
                )
            )
    return jobs, skipped


def _select_fields_from_fieldset(fieldset, path: Path, short_name: str):
    selected = fieldset.sel(paramId=INPUT_PARAM_IDS[short_name])
    if len(selected) == 0:
        raise MissingFieldError(f"Missing {short_name} ({INPUT_PARAM_IDS[short_name]}) in {path}")
    return selected


def _stack_3d_from_fieldset(fieldset, path: Path, short_name: str) -> np.ndarray:
    selected = _select_fields_from_fieldset(fieldset, path, short_name)
    return np.stack([field.to_numpy(flatten=False) for field in selected], axis=0)


def _read_2d_from_fieldset(fieldset, path: Path, short_name: str):
    selected = _select_fields_from_fieldset(fieldset, path, short_name)
    return _read_2d_from_selected(selected, path, short_name)


def _read_2d_from_selected(selected, path: Path, short_name: str):
    if len(selected) != 1:
        raise MissingFieldError(f"Expected one field for {short_name} in {path}, found {len(selected)}")
    field = selected[0]
    return field.to_numpy(flatten=False), field


def load_member_hour_legacy(
    job: MemberHourJob,
    *,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> tuple[GridInputs, LegacyTemplateField, VerticalLevelSelection]:
    bootstrap_eccodes_definitions()

    current_fieldset = from_source("file", str(job.current_file))
    constants_fieldset = from_source("file", str(job.constants_file))
    previous_fieldset = from_source("file", str(job.previous_file)) if job.previous_file is not None else None

    temperature_k = _stack_3d_from_fieldset(current_fieldset, job.current_file, "T")
    pressure_pa = _stack_3d_from_fieldset(current_fieldset, job.current_file, "P")
    specific_humidity = _stack_3d_from_fieldset(current_fieldset, job.current_file, "QV")
    half_level_height_m = _stack_3d_from_fieldset(constants_fieldset, job.constants_file, "HHL")

    selection = derive_vertical_level_selection(half_level_height_m, vertical_cutoff_m)
    if selection.full_level_start > 0:
        temperature_k = temperature_k[selection.full_level_start :]
        pressure_pa = pressure_pa[selection.full_level_start :]
        specific_humidity = specific_humidity[selection.full_level_start :]
        half_level_height_m = half_level_height_m[selection.half_level_start :]

    total_precip_current, template_field = _read_2d_from_fieldset(current_fieldset, job.current_file, "TOT_PREC")
    if job.previous_file is None:
        total_precip_mm = total_precip_current
    else:
        assert previous_fieldset is not None
        total_precip_previous, _ = _read_2d_from_fieldset(previous_fieldset, job.previous_file, "TOT_PREC")
        total_precip_mm = total_precip_current - total_precip_previous

    ground_temperature_k, _ = _read_2d_from_fieldset(current_fieldset, job.current_file, "T_G")
    ground_temperature_c = ground_temperature_k - 273.15

    return (
        GridInputs(
            temperature_k=temperature_k,
            pressure_pa=pressure_pa,
            specific_humidity=specific_humidity,
            half_level_height_m=half_level_height_m,
            total_precip_mm=total_precip_mm,
            ground_temperature_c=ground_temperature_c,
        ),
        LegacyTemplateField(field=template_field),
        selection,
    )


def _reshape_message_values(handle, values: np.ndarray) -> np.ndarray:
    for nrows_key, ncols_key in (("Nj", "Ni"), ("Ny", "Nx")):
        if not (eccodes.codes_is_defined(handle, nrows_key) and eccodes.codes_is_defined(handle, ncols_key)):
            continue
        try:
            nrows = int(eccodes.codes_get_long(handle, nrows_key))
            ncols = int(eccodes.codes_get_long(handle, ncols_key))
        except Exception:
            continue
        if nrows > 0 and ncols > 0 and nrows * ncols == values.size:
            return values.reshape(nrows, ncols)
    return values


def derive_vertical_level_selection(
    half_level_height_m: np.ndarray,
    vertical_cutoff_m: float | None,
) -> VerticalLevelSelection:
    if vertical_cutoff_m is None:
        return VerticalLevelSelection(
            full_level_start=0,
            half_level_start=0,
            retained_full_levels=int(half_level_height_m.shape[0] - 1),
        )
    cutoff = float(vertical_cutoff_m)
    if not np.isfinite(cutoff):
        raise ValueError(f"vertical_cutoff_m must be finite, got {vertical_cutoff_m!r}")

    half_level_height_m = np.asarray(half_level_height_m, dtype=float)
    if half_level_height_m.ndim < 2:
        raise ValueError(
            "half_level_height_m must have shape (half_level, npoint) or (half_level, y, x), "
            f"got {half_level_height_m.shape}"
        )
    flat = half_level_height_m.reshape(half_level_height_m.shape[0], -1)
    full_level_height_m = 0.5 * (flat[:-1] + flat[1:])
    domain_min_full_level_height_m = np.nanmin(full_level_height_m, axis=1)
    keep_mask = domain_min_full_level_height_m <= cutoff
    if np.any(keep_mask):
        full_level_start = int(np.argmax(keep_mask))
    else:
        full_level_start = 0

    retained_full_levels = int(full_level_height_m.shape[0] - full_level_start)
    if retained_full_levels <= 0:
        raise ValueError(
            f"vertical_cutoff_m={vertical_cutoff_m} removed all full levels; "
            "choose a higher cutoff or disable truncation"
        )

    return VerticalLevelSelection(
        full_level_start=full_level_start,
        half_level_start=full_level_start,
        retained_full_levels=retained_full_levels,
    )


def _scan_grib_file_indexed(
    path: Path,
    required_fields: Iterable[str],
    *,
    level_start_by_name: dict[str, int] | None = None,
    capture_template_for: str | None = None,
) -> tuple[dict[str, np.ndarray], GribTemplateMessage | None]:
    required_names = tuple(required_fields)
    level_start_by_name = level_start_by_name or {}
    arrays_3d: dict[str, list[np.ndarray]] = {name: [] for name in required_names if name in THREE_D_FIELDS}
    arrays_2d: dict[str, np.ndarray] = {}
    template: GribTemplateMessage | None = None

    index = _open_param_id_index(path)
    try:
        for short_name in required_names:
            eccodes.codes_index_select_long(index, "paramId", INPUT_PARAM_IDS[short_name])
            message_index = 0
            while True:
                gid = eccodes.codes_new_from_index(index)
                if gid is None:
                    break
                try:
                    if short_name in THREE_D_FIELDS and message_index < level_start_by_name.get(short_name, 0):
                        message_index += 1
                        continue

                    values = np.asarray(eccodes.codes_get_values(gid), dtype=float)
                    values = _reshape_message_values(gid, values)

                    if short_name in THREE_D_FIELDS:
                        arrays_3d[short_name].append(values)
                    else:
                        arrays_2d[short_name] = values
                        if capture_template_for == short_name and template is None:
                            template = GribTemplateMessage(
                                message_bytes=eccodes.codes_get_message(gid),
                                values_shape=tuple(values.shape),
                            )
                    message_index += 1
                finally:
                    eccodes.codes_release(gid)
    finally:
        eccodes.codes_index_release(index)

    result: dict[str, np.ndarray] = {}
    for short_name in required_names:
        if short_name in THREE_D_FIELDS:
            messages = arrays_3d[short_name]
            if not messages:
                raise MissingFieldError(f"Missing {short_name} ({INPUT_PARAM_IDS[short_name]}) in {path}")
            result[short_name] = np.stack(messages, axis=0)
        else:
            if short_name not in arrays_2d:
                raise MissingFieldError(f"Missing {short_name} ({INPUT_PARAM_IDS[short_name]}) in {path}")
            result[short_name] = arrays_2d[short_name]

    return result, template


def _scan_grib_file_sequential(
    path: Path,
    required_fields: Iterable[str],
    *,
    level_start_by_name: dict[str, int] | None = None,
    capture_template_for: str | None = None,
) -> tuple[dict[str, np.ndarray], GribTemplateMessage | None]:
    required_names = tuple(required_fields)
    field_by_param = {INPUT_PARAM_IDS[name]: name for name in required_names}
    level_start_by_name = level_start_by_name or {}
    arrays_3d: dict[str, list[np.ndarray]] = {name: [] for name in required_names if name in THREE_D_FIELDS}
    arrays_2d: dict[str, np.ndarray] = {}
    message_indices: dict[str, int] = {name: 0 for name in required_names}
    template: GribTemplateMessage | None = None

    with path.open("rb") as handle:
        while True:
            gid = eccodes.codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                param_id = int(eccodes.codes_get_long(gid, "paramId"))
                short_name = field_by_param.get(param_id)
                if short_name is None:
                    continue

                message_index = message_indices[short_name]
                message_indices[short_name] = message_index + 1

                if short_name in THREE_D_FIELDS and message_index < level_start_by_name.get(short_name, 0):
                    continue

                values = np.asarray(eccodes.codes_get_values(gid), dtype=float)
                values = _reshape_message_values(gid, values)

                if short_name in THREE_D_FIELDS:
                    arrays_3d[short_name].append(values)
                else:
                    arrays_2d[short_name] = values
                    if capture_template_for == short_name and template is None:
                        template = GribTemplateMessage(
                            message_bytes=eccodes.codes_get_message(gid),
                            values_shape=tuple(values.shape),
                        )
            finally:
                eccodes.codes_release(gid)

    result: dict[str, np.ndarray] = {}
    for short_name in required_names:
        if short_name in THREE_D_FIELDS:
            messages = arrays_3d[short_name]
            if not messages:
                raise MissingFieldError(f"Missing {short_name} ({INPUT_PARAM_IDS[short_name]}) in {path}")
            result[short_name] = np.stack(messages, axis=0)
        else:
            if short_name not in arrays_2d:
                raise MissingFieldError(f"Missing {short_name} ({INPUT_PARAM_IDS[short_name]}) in {path}")
            result[short_name] = arrays_2d[short_name]

    return result, template


def _scan_grib_file_fast(
    path: Path,
    required_fields: Iterable[str],
    *,
    level_start_by_name: dict[str, int] | None = None,
    capture_template_for: str | None = None,
) -> tuple[dict[str, np.ndarray], GribTemplateMessage | None]:
    try:
        return _scan_grib_file_indexed(
            path,
            required_fields,
            level_start_by_name=level_start_by_name,
            capture_template_for=capture_template_for,
        )
    except (OSError, MissingFieldError, eccodes.CodesInternalError):
        return _scan_grib_file_sequential(
            path,
            required_fields,
            level_start_by_name=level_start_by_name,
            capture_template_for=capture_template_for,
        )


def load_member_hour_fast(
    job: MemberHourJob,
    *,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
) -> tuple[GridInputs, GribTemplateMessage, VerticalLevelSelection]:
    bootstrap_eccodes_definitions()

    constants_fields, _ = _scan_grib_file_fast(job.constants_file, ("HHL",))
    full_hhl = constants_fields["HHL"]
    selection = derive_vertical_level_selection(full_hhl, vertical_cutoff_m)
    half_level_height_m = full_hhl[selection.half_level_start :]

    current_fields, template = _scan_grib_file_fast(
        job.current_file,
        ("T", "P", "QV", "TOT_PREC", "T_G"),
        level_start_by_name={
            "T": selection.full_level_start,
            "P": selection.full_level_start,
            "QV": selection.full_level_start,
        },
        capture_template_for="TOT_PREC",
    )
    if template is None:
        raise MissingFieldError(f"Missing TOT_PREC ({INPUT_PARAM_IDS['TOT_PREC']}) in {job.current_file}")

    total_precip_current = current_fields["TOT_PREC"]
    if job.previous_file is None:
        total_precip_mm = total_precip_current
    else:
        previous_fields, _ = _scan_grib_file_fast(job.previous_file, ("TOT_PREC",))
        total_precip_mm = total_precip_current - previous_fields["TOT_PREC"]

    return (
        GridInputs(
            temperature_k=current_fields["T"],
            pressure_pa=current_fields["P"],
            specific_humidity=current_fields["QV"],
            half_level_height_m=half_level_height_m,
            total_precip_mm=total_precip_mm,
            ground_temperature_c=current_fields["T_G"] - 273.15,
        ),
        template,
        selection,
    )


def load_member_hour(
    job: MemberHourJob,
) -> tuple[GridInputs, GribTemplateMessage, VerticalLevelSelection]:
    return load_member_hour_fast(job, vertical_cutoff_m=DEFAULT_VERTICAL_CUTOFF_M)


def output_path(output_dir: Path, job: MemberHourJob) -> Path:
    return Path(output_dir) / job.member / f"{job.current_file.name}.ptype.grib2"


def write_output_grib(template_field, categorical_codes: np.ndarray, destination: Path) -> Path:
    bootstrap_eccodes_definitions()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        if isinstance(template_field, GribTemplateMessage):
            handle_id = eccodes.codes_new_from_message(template_field.message_bytes)
            try:
                categorical = np.asarray(categorical_codes, dtype=np.int32)
                if tuple(categorical.shape) != tuple(template_field.values_shape):
                    raise ValueError(
                        f"categorical_codes shape {categorical.shape} does not match template shape {template_field.values_shape}"
                    )
                eccodes.codes_set(handle_id, "discipline", 0)
                eccodes.codes_set(handle_id, "parameterCategory", 1)
                eccodes.codes_set(handle_id, "parameterNumber", 19)
                eccodes.codes_set(handle_id, "packingType", "grid_simple")
                eccodes.codes_set_values(handle_id, categorical.astype(float).reshape(-1))
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=f"{destination.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    eccodes.codes_write(handle_id, handle)
            finally:
                eccodes.codes_release(handle_id)
        else:
            template = template_field.field if isinstance(template_field, LegacyTemplateField) else template_field
            encoder = GribEncoder()
            encoded = encoder.encode(
                values=np.asarray(categorical_codes, dtype=np.int32),
                template=template,
                metadata={
                    "discipline": 0,
                    "parameterCategory": 1,
                    "parameterNumber": 19,
                    "packingType": "grid_simple",
                },
            )
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f"{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                encoded.to_file(handle)
        os.replace(temp_path, destination)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
    return destination


def process_job(
    job: MemberHourJob,
    output_dir: Path,
    *,
    precip_mask_threshold_mm: float = 0.0,
) -> Path:
    threshold = validate_precip_mask_threshold_mm(precip_mask_threshold_mm)
    grid_inputs, template_field, _ = load_member_hour(job)
    categorical_codes = diagnose_grid_categorical(
        grid_inputs,
        precip_mask_threshold_mm=threshold,
    )
    return write_output_grib(template_field, categorical_codes, output_path(output_dir, job))


def report_for_run(input_run: Path) -> dict[str, object]:
    input_run = Path(input_run)
    members = available_members(input_run)
    return {
        "input_run": str(input_run),
        "members": members,
        "hours_by_member": {member: available_steps(input_run / member) for member in members},
        "required_fields": {name: INPUT_PARAM_IDS[name] for name in REQUIRED_INPUT_FIELDS},
        "output_paramId": OUTPUT_PARAM_ID,
        "output_shortName": OUTPUT_SHORT_NAME,
    }
