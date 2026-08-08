"""Resumable quality control, compact repacking, and analysis products for REA PTYPE archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shlex
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import eccodes
import netCDF4
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from earthkit.data import from_source

from .backfill import REA_MODEL
from .backfill import campaign_status as source_campaign_status
from .backfill import load_manifest as load_source_manifest
from .constants import (
    OUTPUT_PARAM_ID,
    OUTPUT_SHORT_NAME,
    PRECIPITATION_TYPE_NAMES,
    PTYPE_BITS_PER_VALUE,
    PrecipitationTypeCode,
)
from .gribio import _materialize_field_list, bootstrap_eccodes_definitions
from .operational import CycleLock, _atomic_write_json
from .provenance import collect_runtime_provenance

LOGGER = logging.getLogger(__name__)
ANALYSIS_MANIFEST_SCHEMA_VERSION = 1
ANALYSIS_MANIFEST_MODE = "rea_l_ch1_analysis_preparation"
ANALYSIS_CONTRACT_SCHEMA_VERSION = 1
REDUCTION_SCHEMA_VERSION = 1
COMPACT_ARCHIVE_PROMOTION_SCHEMA_VERSION = 1
SOURCE_RETIREMENT_SCHEMA_VERSION = 1
COMPACT_ARCHIVE_PROMOTION_FILENAME = "COMPACT_ARCHIVE_PROMOTION.json"
SOURCE_RETIREMENT_FILENAME = "SOURCE_RETIREMENT.json"
CATEGORY_CODES = tuple(int(code) for code in PrecipitationTypeCode)
CATEGORY_NAMES = tuple(PRECIPITATION_TYPE_NAMES[PrecipitationTypeCode(code)] for code in CATEGORY_CODES)
HIGH_IMPACT_CODES = (
    int(PrecipitationTypeCode.FREEZING_RAIN),
    int(PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND),
)
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SEASON_NAMES = ("DJF", "MAM", "JJA", "SON")
SOURCE_METADATA_KEYS = (
    "shortName",
    "paramId",
    "dataDate",
    "dataTime",
    "endStep",
    "validityDate",
    "validityTime",
    "gridType",
    "numberOfDataPoints",
    "numberOfMissing",
    "bitmapPresent",
    "bitsPerValue",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_immutable_json(payload: dict[str, object], path: Path) -> None:
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != payload:
            raise RuntimeError(f"Immutable contract mismatch at {path}")
        return
    _atomic_write_json(payload, path)


def _safe_relative(value: object, *, label: str) -> Path:
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Invalid {label} path {path}")
    return path


def _ensure_separate_roots(source_root: Path, output_root: Path, staging_root: Path) -> None:
    source = source_root.resolve()
    output = output_root.resolve()
    staging = staging_root.resolve()
    if output == source or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("analysis output_root and source archive root must not overlap")
    if staging == source or staging.is_relative_to(source) or source.is_relative_to(staging):
        raise ValueError("analysis staging_root and source archive root must not overlap")
    if staging == output or staging.is_relative_to(output) or output.is_relative_to(staging):
        raise ValueError("analysis staging_root and output_root must not overlap")


def _period_paths(period: dict[str, object]) -> dict[str, str]:
    source_archive = _safe_relative(period["archive"], label="source archive")
    period_name = str(period["period"])
    return {
        "source_archive": str(source_archive),
        "compact_archive": str(Path("compact") / source_archive),
        "monthly_statistics": str(Path("monthly") / f"ptype_counts_{period_name}.nc"),
        "hourly_counts": str(Path("monthly") / f"ptype_hourly_counts_{period_name}.parquet"),
    }


def _parse_period(value: str, *, label: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y%m")
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYYMM: {value}") from exc
    if parsed.strftime("%Y%m") != value:
        raise ValueError(f"{label} must use YYYYMM: {value}")
    return value


def build_analysis_manifest(
    *,
    source_manifest_path: Path,
    manifest_path: Path,
    output_root: Path,
    staging_root: Path,
    start_period: str | None = None,
    end_period: str | None = None,
) -> dict[str, object]:
    source_manifest_path = source_manifest_path.resolve()
    source_manifest = load_source_manifest(source_manifest_path)
    source_status = source_campaign_status(manifest_path=source_manifest_path, verify_outputs=False)
    if source_status.get("complete") is not True:
        raise RuntimeError("source backfill campaign must be complete before analysis preparation")
    source_root = Path(str(source_manifest["output_root"])).resolve()
    resolved_output = output_root.resolve()
    resolved_staging = staging_root.resolve()
    _ensure_separate_roots(source_root, resolved_output, resolved_staging)
    raw_periods = source_manifest.get("periods")
    if not isinstance(raw_periods, list) or not raw_periods:
        raise RuntimeError("source manifest has no monthly periods")
    lower = _parse_period(start_period, label="start_period") if start_period is not None else None
    upper = _parse_period(end_period, label="end_period") if end_period is not None else None
    if lower is not None and upper is not None and lower > upper:
        raise ValueError(f"start_period {lower} is after end_period {upper}")
    periods: list[dict[str, object]] = []
    for source_index, raw_period in enumerate(raw_periods):
        if not isinstance(raw_period, dict):
            raise RuntimeError(f"invalid source period at index {source_index}")
        period_name = _parse_period(str(raw_period["period"]), label=f"source period {source_index}")
        if (lower is not None and period_name < lower) or (upper is not None and period_name > upper):
            continue
        paths = _period_paths(raw_period)
        periods.append(
            {
                "index": len(periods),
                "source_index": source_index,
                "period": period_name,
                "dates": list(raw_period["dates"]),
                "message_count": int(raw_period["message_count"]),
                **paths,
            }
        )
    if not periods:
        requested = f"{lower or 'first'}..{upper or 'last'}"
        raise ValueError(f"no source periods match requested range {requested}")
    selected_dates = [str(date) for period in periods for date in cast(list[object], period["dates"])]
    if not selected_dates:
        raise RuntimeError("selected source periods contain no cycle dates")
    first_cycle = datetime.strptime(selected_dates[0], "%Y%m%d").replace(tzinfo=timezone.utc)
    last_cycle = datetime.strptime(selected_dates[-1], "%Y%m%d").replace(tzinfo=timezone.utc)
    manifest: dict[str, object] = {
        "schema_version": ANALYSIS_MANIFEST_SCHEMA_VERSION,
        "mode": ANALYSIS_MANIFEST_MODE,
        "model": REA_MODEL,
        "source_manifest_path": str(source_manifest_path),
        "source_manifest_sha256": _sha256_file(source_manifest_path),
        "source_archive_root": str(source_root),
        "output_root": str(resolved_output),
        "staging_root": str(resolved_staging),
        "manifest_path": str(manifest_path.resolve()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "cycle_date_range": {
            "start": first_cycle.strftime("%Y%m%d"),
            "end": last_cycle.strftime("%Y%m%d"),
            "inclusive": True,
        },
        "valid_time_coverage": {
            "first_interval_end": (first_cycle + timedelta(hours=1)).isoformat(),
            "last_interval_end": (last_cycle + timedelta(hours=24)).isoformat(),
        },
        "source_period_range": {
            "start": str(cast(dict[str, object], raw_periods[0])["period"]),
            "end": str(cast(dict[str, object], raw_periods[-1])["period"]),
        },
        "selected_period_range": {"start": periods[0]["period"], "end": periods[-1]["period"]},
        "source_diagnostic_algorithm": source_manifest["diagnostic_algorithm"],
        "category_codes": list(CATEGORY_CODES),
        "category_names": list(CATEGORY_NAMES),
        "high_impact_codes": list(HIGH_IMPACT_CODES),
        "compact_bits_per_value": PTYPE_BITS_PER_VALUE,
        "periods": periods,
        "final_products": {
            "hourly_counts": "ptype_hourly_counts.parquet",
            "frequency": "ptype_frequency.nc",
            "high_impact_events": "high_impact_events.parquet",
            "freezing_rain_map": "maps/freezing_rain_frequency.nc",
            "quality_report": "DATA_QUALITY_REPORT.json",
            "quality_report_markdown": "DATA_QUALITY_REPORT.md",
            "grid": "grid.nc",
            "reduction_receipt": "REDUCTION.json",
        },
    }
    _atomic_write_json(manifest, manifest_path)
    return manifest


def load_analysis_manifest(path: Path) -> dict[str, object]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != ANALYSIS_MANIFEST_SCHEMA_VERSION or manifest.get("mode") != ANALYSIS_MANIFEST_MODE:
        raise RuntimeError(f"Unsupported analysis manifest: {path}")
    source_manifest_path = Path(str(manifest.get("source_manifest_path")))
    if not source_manifest_path.is_file() or _sha256_file(source_manifest_path) != manifest.get("source_manifest_sha256"):
        raise RuntimeError(f"Source manifest changed or is unavailable: {source_manifest_path}")
    periods = manifest.get("periods")
    if not isinstance(periods, list) or not periods:
        raise RuntimeError(f"Analysis manifest contains no periods: {path}")
    for index, period in enumerate(periods):
        if not isinstance(period, dict) or period.get("index") != index:
            raise RuntimeError(f"Invalid analysis period at index {index}")
    return manifest


def _manifest_period(manifest: dict[str, object], index: int) -> dict[str, object]:
    periods = manifest["periods"]
    if not isinstance(periods, list) or not 0 <= index < len(periods):
        raise IndexError(f"analysis index {index} is outside 0..{len(periods) - 1 if isinstance(periods, list) else -1}")
    period = periods[index]
    if not isinstance(period, dict):
        raise RuntimeError(f"invalid analysis period at index {index}")
    return period


def _output_path(manifest: dict[str, object], period: dict[str, object], key: str) -> Path:
    return Path(str(manifest["output_root"])) / _safe_relative(period[key], label=key)


def _receipt_path(manifest_path: Path, period: dict[str, object]) -> Path:
    return manifest_path.parent / "receipts" / f"{int(str(period['index'])):05d}-{period['period']}.json"


def _promotion_paths(manifest_path: Path, manifest: dict[str, object]) -> tuple[Path, Path]:
    filename = COMPACT_ARCHIVE_PROMOTION_FILENAME
    return manifest_path.parent / filename, Path(str(manifest["output_root"])) / filename


def _retirement_paths(manifest_path: Path, manifest: dict[str, object]) -> tuple[Path, Path]:
    filename = SOURCE_RETIREMENT_FILENAME
    return manifest_path.parent / filename, Path(str(manifest["output_root"])) / filename


def _ensure_contract(manifest: dict[str, object]) -> None:
    contract = {
        "schema_version": ANALYSIS_CONTRACT_SCHEMA_VERSION,
        "mode": ANALYSIS_MANIFEST_MODE,
        "model": REA_MODEL,
        "source_manifest_path": manifest["source_manifest_path"],
        "source_manifest_sha256": manifest["source_manifest_sha256"],
        "source_archive_root": manifest["source_archive_root"],
        "compact_bits_per_value": PTYPE_BITS_PER_VALUE,
        "constant_field_bits_per_value": 0,
        "category_codes": list(CATEGORY_CODES),
        "high_impact_codes": list(HIGH_IMPACT_CODES),
        "valid_time_grouping": "GRIB validityDate and validityTime",
        "cell_area_contract": "unavailable; report grid-cell counts and domain fractions, not square kilometres",
    }
    path = Path(str(manifest["output_root"])) / "ANALYSIS_CONTRACT.json"
    if path.is_file():
        if json.loads(path.read_text(encoding="utf-8")) != contract:
            raise RuntimeError(f"Analysis contract mismatch at {path}; use a new output root")
        return
    _atomic_write_json(contract, path)


def _optional_get(message_id: int, key: str, default: object = None) -> object:
    try:
        return eccodes.codes_get(message_id, key)
    except Exception:
        return default


def _message_metadata(message_id: int) -> dict[str, object]:
    metadata = {key: _optional_get(message_id, key) for key in SOURCE_METADATA_KEYS}
    metadata["uuidOfHGrid"] = _optional_get(message_id, "uuidOfHGrid", "")
    return metadata


def _expected_messages(period: dict[str, object]) -> list[tuple[str, int]]:
    dates = period["dates"]
    if not isinstance(dates, list):
        raise RuntimeError(f"invalid dates for analysis period {period.get('period')}")
    return [(str(date), step) for date in dates for step in range(1, 25)]


def _valid_time(date: str, step: int) -> datetime:
    return datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc) + timedelta(hours=step)


def _validate_metadata(metadata: dict[str, object], date: str, step: int, *, position: int, path: Path) -> datetime:
    valid = _valid_time(date, step)
    expected = {
        "shortName": OUTPUT_SHORT_NAME,
        "paramId": OUTPUT_PARAM_ID,
        "dataDate": int(date),
        "dataTime": 0,
        "endStep": step,
        "validityDate": int(valid.strftime("%Y%m%d")),
        "validityTime": int(valid.strftime("%H%M")),
        "numberOfMissing": 0,
        "bitmapPresent": 0,
    }
    mismatches = {
        key: {"actual": metadata.get(key), "expected": value}
        for key, value in expected.items()
        if str(metadata.get(key)) != str(value)
    }
    if mismatches:
        raise RuntimeError(f"{path} message {position} metadata mismatch: {mismatches}")
    return valid


def _categorical_values(message_id: int, *, position: int, path: Path) -> np.ndarray:
    values = np.asarray(eccodes.codes_get_values(message_id))
    if values.ndim != 1 or not np.all(np.isfinite(values)):
        raise RuntimeError(f"{path} message {position} has invalid value shape or non-finite values")
    rounded = np.rint(values)
    if not np.array_equal(values, rounded):
        raise RuntimeError(f"{path} message {position} contains non-integer categories")
    categorical = rounded.astype(np.uint8)
    allowed = np.zeros(256, dtype=bool)
    allowed[np.asarray(CATEGORY_CODES, dtype=np.uint8)] = True
    invalid = np.unique(categorical[~allowed[categorical]])
    if invalid.size:
        raise RuntimeError(f"{path} message {position} contains invalid PTYPE codes {invalid.tolist()}")
    return categorical


def _hourly_schema() -> pa.Schema:
    fields = [
        pa.field("valid_time", pa.timestamp("s", tz="UTC"), nullable=False),
        pa.field("cycle_date", pa.int32(), nullable=False),
        pa.field("step", pa.int8(), nullable=False),
        pa.field("grid_point_count", pa.int32(), nullable=False),
        pa.field("precip_grid_point_count", pa.int32(), nullable=False),
        pa.field("high_impact_grid_point_count", pa.int32(), nullable=False),
        *(pa.field(f"ptype_{code}_count", pa.int32(), nullable=False) for code in CATEGORY_CODES),
        pa.field("source_bits_per_value", pa.int16(), nullable=False),
        pa.field("source_min", pa.int8(), nullable=False),
        pa.field("source_max", pa.int8(), nullable=False),
        pa.field("decoded_sha256", pa.string(), nullable=False),
        pa.field("all_no_precip", pa.bool_(), nullable=False),
        pa.field("qc_ok", pa.bool_(), nullable=False),
    ]
    return pa.schema(fields, metadata={b"grain": b"one row per valid hour", b"timezone": b"UTC"})


def _atomic_write_parquet(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp.parquet", dir=path.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        pq.write_table(table, temporary_path, compression="zstd", version="2.6")
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _atomic_monthly_netcdf(
    *,
    contributions: dict[str, np.ndarray],
    valid_hours: dict[str, int],
    grid_uuid: str,
    path: Path,
) -> None:
    periods = sorted(contributions)
    if not periods:
        raise RuntimeError("monthly task produced no valid-time contributions")
    npoint = next(iter(contributions.values())).shape[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp.nc", dir=path.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        with netCDF4.Dataset(temporary_path, "w", format="NETCDF4") as dataset:
            dataset.createDimension("valid_period", len(periods))
            dataset.createDimension("category", len(CATEGORY_CODES))
            dataset.createDimension("cell", npoint)
            dataset.setncattr("grid_uuid", grid_uuid)
            dataset.setncattr("grain", "valid month, precipitation type, grid cell")
            dataset.setncattr("valid_time_grouping", "GRIB validityDate and validityTime")
            period_var = dataset.createVariable("valid_period", str, ("valid_period",))
            period_var[:] = np.asarray(periods, dtype=object)
            category_var = dataset.createVariable("category", "i1", ("category",))
            category_var[:] = np.asarray(CATEGORY_CODES, dtype=np.int8)
            category_var.setncattr("category_names", json.dumps(CATEGORY_NAMES))
            hours_var = dataset.createVariable("valid_hours", "u2", ("valid_period",))
            hours_var[:] = np.asarray([valid_hours[period] for period in periods], dtype=np.uint16)
            count_var = dataset.createVariable(
                "ptype_count",
                "u2",
                ("valid_period", "category", "cell"),
                zlib=True,
                complevel=4,
                shuffle=True,
                chunksizes=(1, 1, min(npoint, 65536)),
            )
            for index, period in enumerate(periods):
                count_var[index, :, :] = contributions[period]
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _grid_from_message(message_bytes: bytes) -> tuple[np.ndarray, np.ndarray]:
    with tempfile.NamedTemporaryFile(suffix=".grib2") as handle:
        handle.write(message_bytes)
        handle.flush()
        field = _materialize_field_list(from_source("file", handle.name))[0]
        latitude = np.asarray(field.geography.latitudes(), dtype=np.float64).reshape(-1)
        longitude = np.asarray(field.geography.longitudes(), dtype=np.float64).reshape(-1)
    return latitude, longitude


def _ensure_grid_file(manifest: dict[str, object], message_bytes: bytes, grid_uuid: str, npoint: int) -> Path:
    output_root = Path(str(manifest["output_root"]))
    grid_path = output_root / "grid.nc"
    lock = CycleLock(output_root / ".grid.lock", timeout_s=600.0)
    lock.acquire()
    try:
        if grid_path.is_file():
            with netCDF4.Dataset(grid_path) as dataset:
                if len(dataset.dimensions["cell"]) != npoint or str(dataset.getncattr("grid_uuid")) != grid_uuid:
                    raise RuntimeError(f"Grid contract mismatch at {grid_path}")
            return grid_path
        latitude, longitude = _grid_from_message(message_bytes)
        if latitude.size != npoint or longitude.size != npoint:
            raise RuntimeError("decoded grid coordinates do not match numberOfDataPoints")
        grid_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".grid.", suffix=".tmp.nc", dir=grid_path.parent)
        os.close(fd)
        temporary_path = Path(temporary)
        try:
            with netCDF4.Dataset(temporary_path, "w", format="NETCDF4") as dataset:
                dataset.createDimension("cell", npoint)
                dataset.setncattr("grid_uuid", grid_uuid)
                dataset.setncattr("grid_type", "unstructured_grid")
                dataset.setncattr("cell_area_available", 0)
                dataset.setncattr("cell_area_note", "Physical cell areas are not encoded in the source PTYPE GRIB")
                lat_var = dataset.createVariable("latitude", "f8", ("cell",), zlib=True, complevel=4, shuffle=True)
                lon_var = dataset.createVariable("longitude", "f8", ("cell",), zlib=True, complevel=4, shuffle=True)
                lat_var.units = "degrees_north"
                lon_var.units = "degrees_east"
                lat_var[:] = latitude
                lon_var[:] = longitude
            os.replace(temporary_path, grid_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return grid_path
    finally:
        lock.release()


def _validate_compact_archive(path: Path, period: dict[str, object]) -> dict[str, object]:
    bootstrap_eccodes_definitions()
    expected = _expected_messages(period)
    decoded_digest = hashlib.sha256()
    byte_digest = hashlib.sha256()
    message_count = 0
    grid_uuid = ""
    npoint = 0
    bits_per_value_counts: Counter[int] = Counter()
    with path.open("rb") as handle:
        while True:
            message_id = eccodes.codes_grib_new_from_file(handle)
            if message_id is None:
                break
            try:
                if message_count >= len(expected):
                    raise RuntimeError(f"compact archive has more than {len(expected)} messages: {path}")
                date, step = expected[message_count]
                metadata = _message_metadata(message_id)
                _validate_metadata(metadata, date, step, position=message_count + 1, path=path)
                values = _categorical_values(message_id, position=message_count + 1, path=path)
                byte_digest.update(eccodes.codes_get_message(message_id))
                bits_per_value = int(str(metadata["bitsPerValue"]))
                constant = bool(np.all(values == values[0]))
                expected_bits = 0 if constant else PTYPE_BITS_PER_VALUE
                if bits_per_value != expected_bits:
                    raise RuntimeError(
                        f"compact archive message {message_count + 1} has bitsPerValue={bits_per_value}; "
                        f"expected {expected_bits} for {'constant' if constant else 'non-constant'} values"
                    )
                bits_per_value_counts[bits_per_value] += 1
                decoded_digest.update(values.tobytes())
                current_uuid = str(metadata.get("uuidOfHGrid") or "")
                current_npoint = int(str(metadata["numberOfDataPoints"]))
                if message_count == 0:
                    grid_uuid, npoint = current_uuid, current_npoint
                elif current_uuid != grid_uuid or current_npoint != npoint:
                    raise RuntimeError(f"compact archive grid changed at message {message_count + 1}")
                message_count += 1
            finally:
                eccodes.codes_release(message_id)
    if message_count != len(expected):
        raise RuntimeError(f"compact archive has {message_count} messages; expected {len(expected)}")
    return {
        "message_count": message_count,
        "decoded_sha256": decoded_digest.hexdigest(),
        "sha256": byte_digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "grid_uuid": grid_uuid,
        "number_of_grid_points": npoint,
        "bits_per_value_counts": {str(key): value for key, value in sorted(bits_per_value_counts.items())},
    }


def _validate_source_archive(path: Path, period: dict[str, object]) -> dict[str, object]:
    bootstrap_eccodes_definitions()
    expected = _expected_messages(period)
    decoded_digest = hashlib.sha256()
    byte_digest = hashlib.sha256()
    message_count = 0
    grid_uuid = ""
    npoint = 0
    bits_per_value_counts: Counter[int] = Counter()
    with path.open("rb") as handle:
        while True:
            message_id = eccodes.codes_grib_new_from_file(handle)
            if message_id is None:
                break
            try:
                if message_count >= len(expected):
                    raise RuntimeError(f"source archive has more than {len(expected)} messages: {path}")
                date, step = expected[message_count]
                metadata = _message_metadata(message_id)
                _validate_metadata(metadata, date, step, position=message_count + 1, path=path)
                values = _categorical_values(message_id, position=message_count + 1, path=path)
                byte_digest.update(eccodes.codes_get_message(message_id))
                decoded_digest.update(values.tobytes())
                bits_per_value_counts[int(str(metadata["bitsPerValue"]))] += 1
                current_uuid = str(metadata.get("uuidOfHGrid") or "")
                current_npoint = int(str(metadata["numberOfDataPoints"]))
                if message_count == 0:
                    grid_uuid, npoint = current_uuid, current_npoint
                elif current_uuid != grid_uuid or current_npoint != npoint:
                    raise RuntimeError(f"source archive grid changed at message {message_count + 1}")
                message_count += 1
            finally:
                eccodes.codes_release(message_id)
    if message_count != len(expected):
        raise RuntimeError(f"source archive has {message_count} messages; expected {len(expected)}")
    return {
        "message_count": message_count,
        "decoded_sha256": decoded_digest.hexdigest(),
        "sha256": byte_digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "mtime_ns": path.stat().st_mtime_ns,
        "grid_uuid": grid_uuid,
        "number_of_grid_points": npoint,
        "bits_per_value_counts": {str(key): value for key, value in sorted(bits_per_value_counts.items())},
    }


def _load_retired_source_entries(
    *, manifest_path: Path, manifest: dict[str, object]
) -> dict[str, dict[str, object]] | None:
    campaign_promotion_path, output_promotion_path = _promotion_paths(manifest_path, manifest)
    campaign_retirement_path, output_retirement_path = _retirement_paths(manifest_path, manifest)
    if not all(
        path.is_file()
        for path in (campaign_promotion_path, output_promotion_path, campaign_retirement_path, output_retirement_path)
    ):
        return None
    try:
        promotion = json.loads(campaign_promotion_path.read_text(encoding="utf-8"))
        if promotion != json.loads(output_promotion_path.read_text(encoding="utf-8")):
            return None
        retirement = json.loads(campaign_retirement_path.read_text(encoding="utf-8"))
        if retirement != json.loads(output_retirement_path.read_text(encoding="utf-8")):
            return None
        if (
            promotion.get("schema_version") != COMPACT_ARCHIVE_PROMOTION_SCHEMA_VERSION
            or promotion.get("status") != "promoted"
            or promotion.get("analysis_manifest_sha256") != _sha256_file(manifest_path)
            or promotion.get("source_archive_root") != str(Path(str(manifest["source_archive_root"])).resolve())
            or promotion.get("compact_archive_root") != str((Path(str(manifest["output_root"])) / "compact").resolve())
            or retirement.get("schema_version") != SOURCE_RETIREMENT_SCHEMA_VERSION
            or retirement.get("status") != "complete"
            or retirement.get("promotion_sha256") != _sha256_file(campaign_promotion_path)
            or retirement.get("source_archive_root") != promotion.get("source_archive_root")
            or retirement.get("deleted_periods") != len(cast(list[object], promotion.get("periods")))
            or retirement.get("remaining_source_files") != 0
        ):
            return None
        raw_entries = promotion.get("periods")
        if not isinstance(raw_entries, list):
            return None
        entries: dict[str, dict[str, object]] = {}
        for entry in raw_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("period"), str):
                return None
            entries[str(entry["period"])] = entry
        periods = manifest.get("periods")
        if not isinstance(periods, list) or set(entries) != {
            str(period["period"]) for period in periods if isinstance(period, dict)
        }:
            return None
        return entries
    except Exception:
        return None


def _receipt_complete(
    *,
    manifest_path: Path,
    manifest: dict[str, object],
    period: dict[str, object],
    verify_outputs: bool,
) -> bool:
    receipt_path = _receipt_path(manifest_path, period)
    if not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("ok") is not True:
            return False
        source_path = Path(str(manifest["source_archive_root"])) / _safe_relative(
            period["source_archive"], label="source archive"
        )
        source_evidence = receipt.get("source_archive")
        if not isinstance(source_evidence, dict):
            return False
        if source_path.is_file():
            if source_evidence.get("size_bytes") != source_path.stat().st_size:
                return False
        else:
            retired_entries = _load_retired_source_entries(manifest_path=manifest_path, manifest=manifest)
            retired = retired_entries.get(str(period["period"])) if retired_entries is not None else None
            if (
                retired is None
                or retired.get("source_archive") != source_evidence
                or retired.get("decoded_sha256") != receipt.get("decoded_sha256")
            ):
                return False
        for key in ("compact_archive", "monthly_statistics", "hourly_counts"):
            path = _output_path(manifest, period, key)
            evidence = receipt.get(key)
            if not path.is_file() or not isinstance(evidence, dict) or evidence.get("size_bytes") != path.stat().st_size:
                return False
            if verify_outputs and key != "compact_archive" and evidence.get("sha256") != _sha256_file(path):
                return False
        if verify_outputs:
            if source_path.is_file() and source_evidence.get("sha256") != _sha256_file(source_path):
                return False
            compact = _validate_compact_archive(_output_path(manifest, period, "compact_archive"), period)
            if (
                compact["decoded_sha256"] != receipt.get("decoded_sha256")
                or compact["sha256"] != cast(dict[str, object], receipt["compact_archive"]).get("sha256")
            ):
                return False
    except Exception:
        return False
    return True


def run_analysis_task(*, manifest_path: Path, index: int, lock_timeout_s: float = 0.0) -> dict[str, object]:
    bootstrap_eccodes_definitions()
    manifest = load_analysis_manifest(manifest_path)
    period = _manifest_period(manifest, index)
    period_name = str(period["period"])
    staging_root = Path(str(manifest["staging_root"]))
    source_path = Path(str(manifest["source_archive_root"])) / _safe_relative(period["source_archive"], label="source archive")
    compact_path = _output_path(manifest, period, "compact_archive")
    statistics_path = _output_path(manifest, period, "monthly_statistics")
    hourly_path = _output_path(manifest, period, "hourly_counts")
    receipt_path = _receipt_path(manifest_path, period)
    lock = CycleLock(manifest_path.parent / "locks" / f"{index:05d}-{period_name}.lock", timeout_s=lock_timeout_s)
    lock.acquire()
    started = datetime.now(timezone.utc)
    try:
        _ensure_contract(manifest)
        if _receipt_complete(
            manifest_path=manifest_path,
            manifest=manifest,
            period=period,
            verify_outputs=True,
        ):
            return json.loads(receipt_path.read_text(encoding="utf-8"))
        if not source_path.is_file():
            if _load_retired_source_entries(manifest_path=manifest_path, manifest=manifest) is not None:
                raise RuntimeError(
                    f"compact analysis output for {period_name} is invalid and its source archive was intentionally retired; "
                    "restore the source from backup before regeneration"
                )
            raise RuntimeError(f"source monthly archive is missing: {source_path}")
        staging_root.mkdir(parents=True, exist_ok=True)
        compact_path.parent.mkdir(parents=True, exist_ok=True)
        statistics_path.parent.mkdir(parents=True, exist_ok=True)
        hourly_path.parent.mkdir(parents=True, exist_ok=True)
        compact_partial = compact_path.with_name(f".{compact_path.name}.partial")
        compact_partial.unlink(missing_ok=True)
        expected = _expected_messages(period)
        source_sha256 = hashlib.sha256()
        compact_sha256 = hashlib.sha256()
        decoded_sha256 = hashlib.sha256()
        rows: list[dict[str, object]] = []
        contributions: dict[str, np.ndarray] = {}
        valid_hours: defaultdict[str, int] = defaultdict(int)
        category_totals = np.zeros(len(CATEGORY_CODES), dtype=np.uint64)
        code_to_index = np.full(256, -1, dtype=np.int16)
        code_to_index[np.asarray(CATEGORY_CODES, dtype=np.uint8)] = np.arange(len(CATEGORY_CODES), dtype=np.int16)
        cell_indices: np.ndarray | None = None
        grid_contract: tuple[str, str, int] | None = None
        first_message_bytes: bytes | None = None
        source_bits: Counter[int] = Counter()
        all_no_precip_hours = 0
        with source_path.open("rb") as source_handle, compact_partial.open("wb") as compact_handle:
            position = 0
            while True:
                message_id = eccodes.codes_grib_new_from_file(source_handle)
                if message_id is None:
                    break
                try:
                    if position >= len(expected):
                        raise RuntimeError(f"source archive has more than {len(expected)} messages")
                    date, step = expected[position]
                    metadata = _message_metadata(message_id)
                    valid = _validate_metadata(metadata, date, step, position=position + 1, path=source_path)
                    categorical = _categorical_values(message_id, position=position + 1, path=source_path)
                    npoint = int(str(metadata["numberOfDataPoints"]))
                    current_grid = (str(metadata["gridType"]), str(metadata.get("uuidOfHGrid") or ""), npoint)
                    if grid_contract is None:
                        grid_contract = current_grid
                        cell_indices = np.arange(npoint, dtype=np.int32)
                    elif current_grid != grid_contract:
                        raise RuntimeError(f"source archive grid changed at message {position + 1}: {current_grid} != {grid_contract}")
                    if categorical.size != npoint or cell_indices is None:
                        raise RuntimeError(f"source archive message {position + 1} has inconsistent value count")
                    raw_message = eccodes.codes_get_message(message_id)
                    if first_message_bytes is None:
                        first_message_bytes = raw_message
                    source_sha256.update(raw_message)
                    message_decoded_sha256 = hashlib.sha256(categorical.tobytes()).hexdigest()
                    decoded_sha256.update(categorical.tobytes())
                    global_counts = np.bincount(categorical, minlength=256)[np.asarray(CATEGORY_CODES)]
                    category_totals += global_counts.astype(np.uint64)
                    valid_period = valid.strftime("%Y%m")
                    if valid_period not in contributions:
                        contributions[valid_period] = np.zeros((len(CATEGORY_CODES), npoint), dtype=np.uint16)
                    category_indices = code_to_index[categorical]
                    contributions[valid_period][category_indices, cell_indices] += np.uint16(1)
                    valid_hours[valid_period] += 1
                    high_impact_count = int(sum(global_counts[CATEGORY_CODES.index(code)] for code in HIGH_IMPACT_CODES))
                    precip_count = npoint - int(global_counts[CATEGORY_CODES.index(0)])
                    all_no_precip = precip_count == 0
                    all_no_precip_hours += int(all_no_precip)
                    source_bit_count = int(str(metadata["bitsPerValue"]))
                    source_bits[source_bit_count] += 1
                    row: dict[str, object] = {
                        "valid_time": valid,
                        "cycle_date": int(date),
                        "step": step,
                        "grid_point_count": npoint,
                        "precip_grid_point_count": precip_count,
                        "high_impact_grid_point_count": high_impact_count,
                        "source_bits_per_value": source_bit_count,
                        "source_min": int(categorical.min()),
                        "source_max": int(categorical.max()),
                        "decoded_sha256": message_decoded_sha256,
                        "all_no_precip": all_no_precip,
                        "qc_ok": True,
                    }
                    row.update({f"ptype_{code}_count": int(global_counts[i]) for i, code in enumerate(CATEGORY_CODES)})
                    rows.append(row)
                    eccodes.codes_set(message_id, "packingType", "grid_simple")
                    eccodes.codes_set(message_id, "bitsPerValue", PTYPE_BITS_PER_VALUE)
                    eccodes.codes_set_values(message_id, categorical)
                    repacked = np.asarray(eccodes.codes_get_values(message_id)).astype(np.uint8)
                    packed_bits = int(eccodes.codes_get(message_id, "bitsPerValue"))
                    expected_bits = 0 if bool(np.all(categorical == categorical[0])) else PTYPE_BITS_PER_VALUE
                    if packed_bits != expected_bits or not np.array_equal(categorical, repacked):
                        raise RuntimeError(f"four-bit in-memory verification failed at message {position + 1}")
                    packed_message = eccodes.codes_get_message(message_id)
                    compact_handle.write(packed_message)
                    compact_sha256.update(packed_message)
                    position += 1
                finally:
                    eccodes.codes_release(message_id)
            compact_handle.flush()
            os.fsync(compact_handle.fileno())
        if position != len(expected):
            raise RuntimeError(f"source archive has {position} messages; expected {len(expected)}")
        if grid_contract is None or first_message_bytes is None:
            raise RuntimeError("source archive contains no GRIB messages")
        compact_check = _validate_compact_archive(compact_partial, period)
        if compact_check["decoded_sha256"] != decoded_sha256.hexdigest() or compact_check["sha256"] != compact_sha256.hexdigest():
            raise RuntimeError("independent compact archive verification failed")
        _ensure_grid_file(manifest, first_message_bytes, grid_contract[1], grid_contract[2])
        table = pa.Table.from_pylist(rows, schema=_hourly_schema())
        _atomic_write_parquet(table, hourly_path)
        _atomic_monthly_netcdf(
            contributions=contributions,
            valid_hours=dict(valid_hours),
            grid_uuid=grid_contract[1],
            path=statistics_path,
        )
        os.replace(compact_partial, compact_path)
        finished = datetime.now(timezone.utc)
        receipt = {
            "schema_version": 1,
            "index": index,
            "period": period_name,
            "status": "complete",
            "ok": True,
            "source_archive": {
                "path": str(source_path),
                "size_bytes": source_path.stat().st_size,
                "sha256": source_sha256.hexdigest(),
                "bits_per_value_counts": {str(key): value for key, value in sorted(source_bits.items())},
            },
            "compact_archive": {
                "path": str(compact_path),
                "size_bytes": compact_path.stat().st_size,
                "sha256": compact_sha256.hexdigest(),
                "nonconstant_bits_per_value": PTYPE_BITS_PER_VALUE,
                "constant_bits_per_value": 0,
                "bits_per_value_counts": compact_check["bits_per_value_counts"],
            },
            "monthly_statistics": {
                "path": str(statistics_path),
                "size_bytes": statistics_path.stat().st_size,
                "sha256": _sha256_file(statistics_path),
                "valid_periods": sorted(valid_hours),
                "valid_hours": dict(sorted(valid_hours.items())),
            },
            "hourly_counts": {
                "path": str(hourly_path),
                "size_bytes": hourly_path.stat().st_size,
                "sha256": _sha256_file(hourly_path),
                "rows": len(rows),
            },
            "message_count": position,
            "number_of_grid_points": grid_contract[2],
            "grid_uuid": grid_contract[1],
            "decoded_sha256": decoded_sha256.hexdigest(),
            "category_totals": {str(code): int(category_totals[i]) for i, code in enumerate(CATEGORY_CODES)},
            "all_no_precip_hours": all_no_precip_hours,
            "compression_ratio": source_path.stat().st_size / compact_path.stat().st_size,
            "provenance": collect_runtime_provenance(),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "wall_s": (finished - started).total_seconds(),
        }
        _atomic_write_json(receipt, receipt_path)
        return receipt
    except Exception as exc:
        for path in (compact_path.with_name(f".{compact_path.name}.partial"),):
            path.unlink(missing_ok=True)
        receipt = {
            "schema_version": 1,
            "index": index,
            "period": period_name,
            "status": "critical",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(receipt, receipt_path)
        raise
    finally:
        lock.release()


def analysis_status(*, manifest_path: Path, verify_outputs: bool = False) -> dict[str, object]:
    manifest = load_analysis_manifest(manifest_path)
    periods = manifest["periods"]
    if not isinstance(periods, list):
        raise RuntimeError("analysis manifest periods are invalid")
    complete: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    for period in periods:
        if not isinstance(period, dict):
            raise RuntimeError("analysis manifest period is invalid")
        receipt_path = _receipt_path(manifest_path, period)
        status: object = None
        if receipt_path.is_file():
            try:
                status = json.loads(receipt_path.read_text(encoding="utf-8")).get("status")
            except Exception:
                status = None
        name = str(period["period"])
        if status == "critical":
            failed.append(name)
        elif _receipt_complete(
            manifest_path=manifest_path,
            manifest=manifest,
            period=period,
            verify_outputs=verify_outputs,
        ):
            complete.append(name)
        else:
            pending.append(name)
    output_root = Path(str(manifest["output_root"]))
    retired_entries = _load_retired_source_entries(manifest_path=manifest_path, manifest=manifest)
    reduction_path = output_root / "REDUCTION.json"
    reduction_complete = False
    if reduction_path.is_file():
        try:
            reduction = json.loads(reduction_path.read_text(encoding="utf-8"))
            reduction_complete = reduction.get("ok") is True
            if reduction_complete and verify_outputs:
                products = reduction.get("products")
                if not isinstance(products, dict):
                    reduction_complete = False
                else:
                    for evidence in products.values():
                        if not isinstance(evidence, dict):
                            reduction_complete = False
                            break
                        path = Path(str(evidence.get("path")))
                        if (
                            not path.is_file()
                            or evidence.get("size_bytes") != path.stat().st_size
                            or evidence.get("sha256") != _sha256_file(path)
                        ):
                            reduction_complete = False
                            break
        except Exception:
            reduction_complete = False
    result = {
        "schema_version": 1,
        "manifest_path": str(manifest_path.resolve()),
        "total_periods": len(periods),
        "complete_periods": len(complete),
        "failed_periods": len(failed),
        "pending_periods": len(pending),
        "tasks_complete": not failed and not pending,
        "reduction_complete": reduction_complete,
        "complete": not failed and not pending and reduction_complete,
        "failed_period_names": failed,
        "pending_period_names": pending,
        "verified_outputs": verify_outputs,
        "source_retired": retired_entries is not None,
        "canonical_compact_archive_root": str((output_root / "compact").resolve()) if retired_entries is not None else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(result, manifest_path.parent / "analysis-status.json")
    return result


def _verify_file_evidence(path: Path, evidence: object, *, label: str) -> None:
    if not isinstance(evidence, dict):
        raise RuntimeError(f"{label} evidence is invalid")
    if not path.is_file():
        raise RuntimeError(f"{label} is missing: {path}")
    if path.is_symlink():
        raise RuntimeError(f"{label} must not be a symlink: {path}")
    if Path(str(evidence.get("path"))).resolve() != path.resolve():
        raise RuntimeError(f"{label} receipt path does not match {path}")
    if evidence.get("size_bytes") != path.stat().st_size or evidence.get("sha256") != _sha256_file(path):
        raise RuntimeError(f"{label} changed after its receipt was written: {path}")


def _validate_full_source_selection(manifest: dict[str, object]) -> None:
    source_manifest_path = Path(str(manifest["source_manifest_path"]))
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_periods = source_manifest.get("periods")
    analysis_periods = manifest.get("periods")
    if not isinstance(source_periods, list) or not isinstance(analysis_periods, list):
        raise RuntimeError("source retirement requires valid source and analysis period lists")
    source_contract = [
        (
            str(period.get("period")),
            str(period.get("archive")),
            int(str(period.get("message_count"))),
            tuple(str(date) for date in cast(list[object], period.get("dates"))),
        )
        for period in source_periods
        if isinstance(period, dict) and isinstance(period.get("dates"), list)
    ]
    analysis_contract = [
        (
            str(period.get("period")),
            str(period.get("source_archive")),
            int(str(period.get("message_count"))),
            tuple(str(date) for date in cast(list[object], period.get("dates"))),
        )
        for period in analysis_periods
        if isinstance(period, dict) and isinstance(period.get("dates"), list)
    ]
    if len(source_contract) != len(source_periods) or source_contract != analysis_contract:
        raise RuntimeError("source retirement is allowed only when the analysis manifest covers every source month exactly")


def _validate_reduction_products(manifest: dict[str, object]) -> None:
    reduction_path = Path(str(manifest["output_root"])) / "REDUCTION.json"
    if not reduction_path.is_file():
        raise RuntimeError(f"analysis reduction receipt is missing: {reduction_path}")
    reduction = json.loads(reduction_path.read_text(encoding="utf-8"))
    if reduction.get("ok") is not True or not isinstance(reduction.get("products"), dict):
        raise RuntimeError(f"analysis reduction receipt is incomplete: {reduction_path}")
    for name, evidence in cast(dict[str, object], reduction["products"]).items():
        if not isinstance(evidence, dict):
            raise RuntimeError(f"reduction evidence for {name} is invalid")
        _verify_file_evidence(Path(str(evidence.get("path"))), evidence, label=f"reduction product {name}")


def _source_inventory(source_root: Path) -> set[Path]:
    if not source_root.exists():
        return set()
    files: set[Path] = set()
    for path in source_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"source archive tree contains a symlink: {path}")
        if path.is_file():
            files.add(path.resolve())
        elif not path.is_dir():
            raise RuntimeError(f"source archive tree contains an unsupported filesystem entry: {path}")
    return files


def promote_compact_archive(
    *,
    manifest_path: Path,
    confirmed_source_root: Path,
    delete_source: bool = False,
) -> dict[str, object]:
    """Seal the exact compact replacement and optionally retire the original archive."""

    bootstrap_eccodes_definitions()
    manifest_path = manifest_path.resolve()
    manifest = load_analysis_manifest(manifest_path)
    _validate_full_source_selection(manifest)
    manifest_source_root = Path(str(manifest["source_archive_root"]))
    if manifest_source_root.is_symlink() or confirmed_source_root.is_symlink():
        raise RuntimeError("source archive root and confirmed source root must not be symlinks")
    source_root = manifest_source_root.resolve()
    output_root = Path(str(manifest["output_root"])).resolve()
    if confirmed_source_root.resolve() != source_root:
        raise RuntimeError(
            f"confirmed source root {confirmed_source_root.resolve()} does not match manifest source root {source_root}"
        )
    if source_root == Path(source_root.anchor) or len(source_root.parts) < 4:
        raise RuntimeError(f"refusing unsafe source archive root: {source_root}")
    _ensure_separate_roots(source_root, output_root, Path(str(manifest["staging_root"])))
    periods = manifest.get("periods")
    if not isinstance(periods, list) or not periods:
        raise RuntimeError("analysis manifest has no periods")
    _ensure_contract(manifest)
    _validate_reduction_products(manifest)

    campaign_promotion_path, output_promotion_path = _promotion_paths(manifest_path, manifest)
    existing_promotions = [path for path in (campaign_promotion_path, output_promotion_path) if path.is_file()]
    promotion: dict[str, object] | None = None
    if existing_promotions:
        promotion = json.loads(existing_promotions[0].read_text(encoding="utf-8"))
        if any(json.loads(path.read_text(encoding="utf-8")) != promotion for path in existing_promotions[1:]):
            raise RuntimeError("campaign and output promotion contracts disagree")
        if (
            promotion.get("schema_version") != COMPACT_ARCHIVE_PROMOTION_SCHEMA_VERSION
            or promotion.get("status") != "promoted"
            or promotion.get("analysis_manifest_sha256") != _sha256_file(manifest_path)
            or promotion.get("source_archive_root") != str(source_root)
            or promotion.get("compact_archive_root") != str((output_root / "compact").resolve())
        ):
            raise RuntimeError("existing compact archive promotion contract does not match this campaign")

    entries: list[dict[str, object]] = []
    source_bytes = 0
    compact_bytes = 0
    expected_source_files: set[Path] = set()
    prior_entries = {
        str(entry["period"]): entry
        for entry in cast(list[dict[str, object]], promotion.get("periods", []) if promotion is not None else [])
        if isinstance(entry, dict) and isinstance(entry.get("period"), str)
    }
    for raw_period in periods:
        if not isinstance(raw_period, dict):
            raise RuntimeError("analysis manifest period is invalid")
        period = raw_period
        period_name = str(period["period"])
        receipt_path = _receipt_path(manifest_path, period)
        if not receipt_path.is_file():
            raise RuntimeError(f"analysis receipt is missing: {receipt_path}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("ok") is not True:
            raise RuntimeError(f"analysis receipt is incomplete: {receipt_path}")
        source_path = source_root / _safe_relative(period["source_archive"], label="source archive")
        compact_path = _output_path(manifest, period, "compact_archive")
        if compact_path.is_symlink():
            raise RuntimeError(f"compact archive must not be a symlink: {compact_path}")
        expected_source_files.add(source_path.resolve())
        compact_check = _validate_compact_archive(compact_path, period)
        compact_evidence = receipt.get("compact_archive")
        expected_message_count = int(str(period["message_count"]))
        if (
            not isinstance(compact_evidence, dict)
            or Path(str(compact_evidence.get("path"))).resolve() != compact_path.resolve()
            or compact_check["size_bytes"] != compact_evidence.get("size_bytes")
            or compact_check["sha256"] != compact_evidence.get("sha256")
            or compact_check["decoded_sha256"] != receipt.get("decoded_sha256")
            or compact_check["message_count"] != expected_message_count
            or receipt.get("message_count") != expected_message_count
        ):
            raise RuntimeError(f"compact archive does not match receipt for {period_name}")
        for key in ("monthly_statistics", "hourly_counts"):
            _verify_file_evidence(_output_path(manifest, period, key), receipt.get(key), label=f"{period_name} {key}")

        prior = prior_entries.get(period_name)
        if source_path.is_file():
            source_check = _validate_source_archive(source_path, period)
            source_evidence = receipt.get("source_archive")
            if (
                not isinstance(source_evidence, dict)
                or Path(str(source_evidence.get("path"))).resolve() != source_path.resolve()
                or source_check["size_bytes"] != source_evidence.get("size_bytes")
                or source_check["sha256"] != source_evidence.get("sha256")
                or source_check["decoded_sha256"] != receipt.get("decoded_sha256")
                or source_check["decoded_sha256"] != compact_check["decoded_sha256"]
                or source_check["message_count"] != expected_message_count
            ):
                raise RuntimeError(f"source, compact, and receipt evidence disagree for {period_name}")
            source_mtime_ns = source_check["mtime_ns"]
        elif prior is not None and prior.get("source_archive") == receipt.get("source_archive"):
            source_evidence = receipt.get("source_archive")
            source_mtime_ns = prior.get("source_mtime_ns")
        else:
            raise RuntimeError(f"source archive disappeared before a valid promotion was sealed: {source_path}")
        if not isinstance(source_evidence, dict):
            raise RuntimeError(f"source archive evidence is invalid for {period_name}")
        entry = {
            "period": period_name,
            "source_relative_path": str(_safe_relative(period["source_archive"], label="source archive")),
            "compact_relative_path": str(_safe_relative(period["compact_archive"], label="compact archive")),
            "message_count": int(str(receipt["message_count"])),
            "decoded_sha256": receipt["decoded_sha256"],
            "source_archive": source_evidence,
            "source_mtime_ns": source_mtime_ns,
            "compact_archive": compact_evidence,
        }
        if prior is not None and entry != prior:
            raise RuntimeError(f"existing promotion evidence changed for {period_name}")
        entries.append(entry)
        source_bytes += int(str(source_evidence["size_bytes"]))
        compact_bytes += int(str(compact_evidence["size_bytes"]))

    archive_contract_path = source_root / "ARCHIVE_CONTRACT.json"
    archive_contract: dict[str, object]
    if archive_contract_path.is_file():
        archive_contract = {
            "relative_path": "ARCHIVE_CONTRACT.json",
            "size_bytes": archive_contract_path.stat().st_size,
            "sha256": _sha256_file(archive_contract_path),
            "mtime_ns": archive_contract_path.stat().st_mtime_ns,
        }
    elif promotion is not None and isinstance(promotion.get("source_archive_contract"), dict):
        archive_contract = cast(dict[str, object], promotion["source_archive_contract"])
    else:
        raise RuntimeError(f"source archive contract is missing: {archive_contract_path}")
    expected_source_files.add(archive_contract_path.resolve())
    actual_source_files = _source_inventory(source_root)
    if promotion is None and actual_source_files != expected_source_files:
        unexpected = sorted(str(path) for path in actual_source_files - expected_source_files)
        missing = sorted(str(path) for path in expected_source_files - actual_source_files)
        raise RuntimeError(f"source archive inventory mismatch; unexpected={unexpected}, missing={missing}")
    if promotion is not None and not actual_source_files.issubset(expected_source_files):
        unexpected = sorted(str(path) for path in actual_source_files - expected_source_files)
        raise RuntimeError(f"source archive contains unexpected files after promotion: {unexpected}")

    if promotion is None:
        promotion = {
            "schema_version": COMPACT_ARCHIVE_PROMOTION_SCHEMA_VERSION,
            "status": "promoted",
            "analysis_manifest_path": str(manifest_path),
            "analysis_manifest_sha256": _sha256_file(manifest_path),
            "source_manifest_path": manifest["source_manifest_path"],
            "source_manifest_sha256": manifest["source_manifest_sha256"],
            "source_archive_root": str(source_root),
            "compact_archive_root": str((output_root / "compact").resolve()),
            "period_count": len(entries),
            "message_count": sum(int(str(entry["message_count"])) for entry in entries),
            "source_size_bytes": source_bytes,
            "compact_size_bytes": compact_bytes,
            "reclaimed_size_bytes": source_bytes,
            "source_archive_contract": archive_contract,
            "periods": entries,
            "validation": {
                "source_and_compact_byte_sha256": True,
                "source_and_compact_decoded_sha256_equal": True,
                "compact_packing_validated": True,
                "monthly_analysis_products_sha256": True,
                "reduction_products_sha256": True,
                "source_inventory_exact": True,
            },
            "provenance": collect_runtime_provenance(),
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        }
    _write_immutable_json(promotion, campaign_promotion_path)
    _write_immutable_json(promotion, output_promotion_path)
    if not delete_source:
        return promotion
    already_retired = _load_retired_source_entries(manifest_path=manifest_path, manifest=manifest)
    if already_retired is not None:
        if _source_inventory(source_root):
            raise RuntimeError("retirement receipt exists but source archive files remain")
        campaign_retirement_path, _ = _retirement_paths(manifest_path, manifest)
        return cast(dict[str, object], json.loads(campaign_retirement_path.read_text(encoding="utf-8")))

    for entry in cast(list[dict[str, object]], promotion["periods"]):
        source_path = source_root / _safe_relative(entry["source_relative_path"], label="promoted source archive")
        if not source_path.exists():
            continue
        evidence = cast(dict[str, object], entry["source_archive"])
        stat = source_path.stat()
        if stat.st_size != evidence.get("size_bytes"):
            raise RuntimeError(f"source archive changed before deletion: {source_path}")
        if stat.st_mtime_ns != entry.get("source_mtime_ns") and _sha256_file(source_path) != evidence.get("sha256"):
            raise RuntimeError(f"source archive changed before deletion: {source_path}")
        source_path.unlink()
    contract_evidence = cast(dict[str, object], promotion["source_archive_contract"])
    if archive_contract_path.exists():
        stat = archive_contract_path.stat()
        if stat.st_size != contract_evidence.get("size_bytes"):
            raise RuntimeError(f"source archive contract changed before deletion: {archive_contract_path}")
        if stat.st_mtime_ns != contract_evidence.get("mtime_ns") and _sha256_file(archive_contract_path) != contract_evidence.get("sha256"):
            raise RuntimeError(f"source archive contract changed before deletion: {archive_contract_path}")
        archive_contract_path.unlink()
    if _source_inventory(source_root):
        raise RuntimeError(f"source archive still contains files after exact retirement pass: {source_root}")
    if source_root.exists():
        directories = sorted((path for path in source_root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True)
        for directory in directories:
            directory.rmdir()
        source_root.rmdir()
    retirement = {
        "schema_version": SOURCE_RETIREMENT_SCHEMA_VERSION,
        "status": "complete",
        "promotion_path": str(campaign_promotion_path),
        "promotion_sha256": _sha256_file(campaign_promotion_path),
        "source_archive_root": str(source_root),
        "canonical_compact_archive_root": promotion["compact_archive_root"],
        "deleted_periods": len(entries),
        "deleted_source_bytes": source_bytes,
        "deleted_archive_contract_bytes": int(str(archive_contract["size_bytes"])),
        "remaining_source_files": 0,
        "source_root_removed": not source_root.exists(),
        "retired_at": datetime.now(timezone.utc).isoformat(),
    }
    campaign_retirement_path, output_retirement_path = _retirement_paths(manifest_path, manifest)
    _write_immutable_json(retirement, campaign_retirement_path)
    _write_immutable_json(retirement, output_retirement_path)
    retired_entries = _load_retired_source_entries(manifest_path=manifest_path, manifest=manifest)
    if retired_entries is None or len(retired_entries) != len(entries):
        raise RuntimeError("retired-source status contract failed after deletion")
    return retirement


def _monthly_contributions(path: Path) -> list[tuple[str, int, np.ndarray]]:
    with netCDF4.Dataset(path) as dataset:
        periods = [str(value) for value in dataset.variables["valid_period"][:]]
        hours = np.asarray(dataset.variables["valid_hours"][:], dtype=np.uint16)
        return [
            (period, int(hours[index]), np.asarray(dataset.variables["ptype_count"][index, :, :], dtype=np.uint16))
            for index, period in enumerate(periods)
        ]


def _season_index(month: int) -> int:
    if month in (12, 1, 2):
        return 0
    if month in (3, 4, 5):
        return 1
    if month in (6, 7, 8):
        return 2
    return 3


def _create_frequency_dataset(path: Path, *, years: list[int], npoint: int, grid_path: Path) -> netCDF4.Dataset:
    dataset = netCDF4.Dataset(path, "w", format="NETCDF4")
    dataset.createDimension("month", 12)
    dataset.createDimension("season", 4)
    dataset.createDimension("year", len(years))
    dataset.createDimension("category", len(CATEGORY_CODES))
    dataset.createDimension("cell", npoint)
    dataset.setncattr("title", "ICON-REA-L-CH1 precipitation-type occurrence frequencies")
    dataset.setncattr("frequency_units", "percent of valid hours")
    with netCDF4.Dataset(grid_path) as grid:
        dataset.setncattr("grid_uuid", grid.getncattr("grid_uuid"))
        for name in ("latitude", "longitude"):
            source = grid.variables[name]
            variable = dataset.createVariable(name, "f8", ("cell",), zlib=True, complevel=4, shuffle=True)
            variable[:] = source[:]
            variable.setncattr("units", source.getncattr("units"))
    category = dataset.createVariable("category", "i1", ("category",))
    category[:] = np.asarray(CATEGORY_CODES, dtype=np.int8)
    category.setncattr("category_names", json.dumps(CATEGORY_NAMES))
    month = dataset.createVariable("month", "i1", ("month",))
    month[:] = np.arange(1, 13, dtype=np.int8)
    month.setncattr("month_names", json.dumps(MONTH_NAMES))
    season = dataset.createVariable("season", str, ("season",))
    season[:] = np.asarray(SEASON_NAMES, dtype=object)
    year = dataset.createVariable("year", "i2", ("year",))
    year[:] = np.asarray(years, dtype=np.int16)
    dataset.createVariable("monthly_climatology_valid_hours", "u4", ("month",))
    dataset.createVariable("seasonal_climatology_valid_hours", "u4", ("season",))
    dataset.createVariable("annual_valid_hours", "u2", ("year",))
    dataset.createVariable("full_period_valid_hours", "u4")
    chunks = (1, 1, min(npoint, 65536))
    for prefix, dims, dtype in (
        ("monthly_climatology", ("month", "category", "cell"), "u2"),
        ("seasonal_climatology", ("season", "category", "cell"), "u2"),
        ("annual", ("year", "category", "cell"), "u2"),
    ):
        dataset.createVariable(f"{prefix}_count", dtype, dims, zlib=True, complevel=4, shuffle=True, chunksizes=chunks)
        dataset.createVariable(f"{prefix}_frequency_percent", "f4", dims, zlib=True, complevel=4, shuffle=True, chunksizes=chunks)
    dataset.createVariable(
        "full_period_count",
        "u4",
        ("category", "cell"),
        zlib=True,
        complevel=4,
        shuffle=True,
        chunksizes=(1, min(npoint, 65536)),
    )
    dataset.createVariable(
        "full_period_frequency_percent",
        "f4",
        ("category", "cell"),
        zlib=True,
        complevel=4,
        shuffle=True,
        chunksizes=(1, min(npoint, 65536)),
    )
    return dataset


def _finalize_event(event: dict[str, object] | None, events: list[dict[str, object]]) -> None:
    if event is None:
        return
    duration = int(str(event["duration_hours"]))
    event["mean_affected_grid_cells"] = float(str(event["sum_affected_cell_hours"])) / duration
    event["max_affected_domain_fraction_percent"] = (
        100.0 * float(str(event["max_affected_grid_cells"])) / int(str(event["grid_point_count"]))
    )
    events.append(event)


def _source_data_quality(manifest: dict[str, object]) -> dict[str, int]:
    source_manifest_path = Path(str(manifest["source_manifest_path"]))
    totals: defaultdict[str, int] = defaultdict(int)
    periods = manifest.get("periods")
    if not isinstance(periods, list):
        raise RuntimeError("analysis periods are invalid while collecting source data quality")
    receipt_paths: list[Path] = []
    for period in periods:
        if not isinstance(period, dict):
            raise RuntimeError("analysis period is invalid while collecting source data quality")
        source_index = int(str(period.get("source_index", period["index"])))
        receipt_paths.append(source_manifest_path.parent / "receipts" / f"{source_index:05d}-{period['period']}.json")
    for receipt_path in receipt_paths:
        if not receipt_path.is_file():
            raise RuntimeError(f"source campaign receipt is missing: {receipt_path}")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"source campaign receipt is invalid: {receipt_path}") from exc
        for result in receipt.get("daily_results") or []:
            if not isinstance(result, dict) or not isinstance(result.get("data_quality"), dict):
                continue
            for key, value in result["data_quality"].items():
                if isinstance(value, int):
                    totals[str(key)] += value
    return dict(sorted(totals.items()))


def _write_quality_markdown(report: dict[str, object], path: Path) -> None:
    lines = [
        "# PTYPE archive data-quality report",
        "",
        f"- Status: **{report['status']}**",
        f"- Valid-hour grain: `{report['first_valid_time']}` through `{report['last_valid_time']}`",
        f"- Records: {report['hourly_records']:,}",
        f"- Grid points per record: {report['number_of_grid_points']:,}",
        f"- Missing or duplicate valid hours: {report['temporal_gap_or_duplicate_count']}",
        f"- Invalid or missing categorical values: {report['invalid_or_missing_value_count']}",
        f"- Original size: {float(str(report['source_size_bytes'])) / 1e9:.3f} GB",
        f"- Four-bit size: {float(str(report['compact_size_bytes'])) / 1e9:.3f} GB",
        f"- Compression ratio: {float(str(report['compression_ratio'])):.3f}x",
        "",
        "## Category totals",
        "",
        "| Code | Name | Count | Share |",
        "| ---: | --- | ---: | ---: |",
    ]
    totals = cast(dict[str, int], report["category_totals"])
    total_values = int(str(report["hourly_records"])) * int(str(report["number_of_grid_points"]))
    for code, name in zip(CATEGORY_CODES, CATEGORY_NAMES, strict=True):
        count = int(totals[str(code)])
        lines.append(f"| {code} | {name} | {count:,} | {100.0 * count / total_values:.8f}% |")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "All structural and categorical checks are fatal at monthly-task time. Distributional results are descriptive; "
            "they do not by themselves establish physical correctness. Physical event area in square kilometres is not reported "
            "because the source PTYPE GRIB does not encode cell areas.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def reduce_analysis(*, manifest_path: Path) -> dict[str, object]:
    manifest = load_analysis_manifest(manifest_path)
    status = analysis_status(manifest_path=manifest_path, verify_outputs=False)
    if status["tasks_complete"] is not True:
        raise RuntimeError("all monthly analysis tasks must complete before reduction")
    output_root = Path(str(manifest["output_root"]))
    final_products = manifest["final_products"]
    if not isinstance(final_products, dict):
        raise RuntimeError("analysis manifest final products are invalid")
    grid_path = output_root / _safe_relative(final_products["grid"], label="grid")
    with netCDF4.Dataset(grid_path) as grid:
        npoint = len(grid.dimensions["cell"])
        grid_uuid = str(grid.getncattr("grid_uuid"))
    periods = manifest["periods"]
    if not isinstance(periods, list):
        raise RuntimeError("analysis periods are invalid")
    receipts = [json.loads(_receipt_path(manifest_path, period).read_text(encoding="utf-8")) for period in periods if isinstance(period, dict)]
    years = sorted({int(str(period)[:4]) for receipt in receipts for period in receipt["monthly_statistics"]["valid_periods"]})
    frequency_path = output_root / _safe_relative(final_products["frequency"], label="frequency")
    frequency_partial = frequency_path.with_name(f".{frequency_path.name}.partial")
    frequency_partial.unlink(missing_ok=True)
    frequency_path.parent.mkdir(parents=True, exist_ok=True)
    month_counts = np.zeros((12, len(CATEGORY_CODES), npoint), dtype=np.uint16)
    season_counts = np.zeros((4, len(CATEGORY_CODES), npoint), dtype=np.uint16)
    full_counts = np.zeros((len(CATEGORY_CODES), npoint), dtype=np.uint32)
    month_hours = np.zeros(12, dtype=np.uint32)
    season_hours = np.zeros(4, dtype=np.uint32)
    annual_hours = np.zeros(len(years), dtype=np.uint16)
    year_index = {year: index for index, year in enumerate(years)}
    pending_period: str | None = None
    pending_hours = 0
    pending_counts: np.ndarray | None = None
    current_year: int | None = None
    current_year_counts = np.zeros((len(CATEGORY_CODES), npoint), dtype=np.uint16)
    dataset = _create_frequency_dataset(frequency_partial, years=years, npoint=npoint, grid_path=grid_path)

    def finalize_valid_month(period_name: str, hours: int, counts: np.ndarray) -> None:
        nonlocal current_year, current_year_counts
        year = int(period_name[:4])
        month = int(period_name[4:6])
        if current_year is None:
            current_year = year
        if year != current_year:
            idx = year_index[current_year]
            dataset.variables["annual_count"][idx, :, :] = current_year_counts
            dataset.variables["annual_frequency_percent"][idx, :, :] = current_year_counts.astype(np.float32) * (100.0 / annual_hours[idx])
            current_year_counts.fill(0)
            current_year = year
        current_year_counts += counts
        annual_hours[year_index[year]] += np.uint16(hours)
        month_counts[month - 1] += counts
        month_hours[month - 1] += np.uint32(hours)
        season = _season_index(month)
        season_counts[season] += counts
        season_hours[season] += np.uint32(hours)
        full_counts[:] += counts.astype(np.uint32)

    for period in periods:
        if not isinstance(period, dict):
            raise RuntimeError("analysis period is invalid")
        for valid_period, hours, counts in _monthly_contributions(_output_path(manifest, period, "monthly_statistics")):
            if pending_period is None:
                pending_period, pending_hours, pending_counts = valid_period, hours, counts
            elif valid_period == pending_period:
                if pending_counts is None:
                    raise RuntimeError("invalid pending monthly reduction state")
                pending_counts += counts
                pending_hours += hours
            else:
                if pending_counts is None:
                    raise RuntimeError("invalid pending monthly reduction state")
                finalize_valid_month(pending_period, pending_hours, pending_counts)
                pending_period, pending_hours, pending_counts = valid_period, hours, counts
    if pending_period is not None and pending_counts is not None:
        finalize_valid_month(pending_period, pending_hours, pending_counts)
    if current_year is not None:
        idx = year_index[current_year]
        dataset.variables["annual_count"][idx, :, :] = current_year_counts
        dataset.variables["annual_frequency_percent"][idx, :, :] = current_year_counts.astype(np.float32) * (100.0 / annual_hours[idx])
    dataset.variables["annual_valid_hours"][:] = annual_hours
    dataset.variables["monthly_climatology_valid_hours"][:] = month_hours
    dataset.variables["seasonal_climatology_valid_hours"][:] = season_hours
    full_hours = int(annual_hours.astype(np.uint32).sum())
    dataset.variables["full_period_valid_hours"].assignValue(np.uint32(full_hours))
    dataset.variables["monthly_climatology_count"][:] = month_counts
    dataset.variables["seasonal_climatology_count"][:] = season_counts
    dataset.variables["full_period_count"][:] = full_counts
    for index in range(12):
        dataset.variables["monthly_climatology_frequency_percent"][index, :, :] = (
            month_counts[index].astype(np.float32) * (100.0 / month_hours[index])
            if month_hours[index] > 0
            else np.full(month_counts[index].shape, np.nan, dtype=np.float32)
        )
    for index in range(4):
        dataset.variables["seasonal_climatology_frequency_percent"][index, :, :] = (
            season_counts[index].astype(np.float32) * (100.0 / season_hours[index])
            if season_hours[index] > 0
            else np.full(season_counts[index].shape, np.nan, dtype=np.float32)
        )
    dataset.variables["full_period_frequency_percent"][:] = full_counts.astype(np.float32) * (100.0 / full_hours)
    dataset.close()
    os.replace(frequency_partial, frequency_path)

    hourly_path = output_root / _safe_relative(final_products["hourly_counts"], label="hourly counts")
    hourly_partial = hourly_path.with_name(f".{hourly_path.name}.partial")
    hourly_partial.unlink(missing_ok=True)
    writer = pq.ParquetWriter(hourly_partial, _hourly_schema(), compression="zstd", version="2.6")
    events: list[dict[str, object]] = []
    event: dict[str, object] | None = None
    prior_time: datetime | None = None
    gap_or_duplicate_count = 0
    hourly_records = 0
    category_totals = np.zeros(len(CATEGORY_CODES), dtype=np.uint64)
    annual_category_totals: defaultdict[int, np.ndarray] = defaultdict(
        lambda: np.zeros(len(CATEGORY_CODES), dtype=np.uint64)
    )
    all_no_precip_hours = 0
    first_valid_time: datetime | None = None
    last_valid_time: datetime | None = None
    for period in periods:
        if not isinstance(period, dict):
            raise RuntimeError("analysis period is invalid")
        table = pq.read_table(_output_path(manifest, period, "hourly_counts"), schema=_hourly_schema())
        writer.write_table(table)
        for row in table.to_pylist():
            valid = cast(datetime, row["valid_time"])
            if valid.tzinfo is None:
                valid = valid.replace(tzinfo=timezone.utc)
            if first_valid_time is None:
                first_valid_time = valid
            if prior_time is not None and valid != prior_time + timedelta(hours=1):
                gap_or_duplicate_count += 1
            prior_time = valid
            last_valid_time = valid
            hourly_records += 1
            all_no_precip_hours += int(bool(row["all_no_precip"]))
            for category_index, code in enumerate(CATEGORY_CODES):
                count = int(row[f"ptype_{code}_count"])
                category_totals[category_index] += count
                annual_category_totals[valid.year][category_index] += count
            affected = int(row["high_impact_grid_point_count"])
            if affected > 0:
                if event is None or valid != cast(datetime, event["end_time"]) + timedelta(hours=1):
                    _finalize_event(event, events)
                    event = {
                        "event_id": len(events) + 1,
                        "start_time": valid,
                        "end_time": valid,
                        "duration_hours": 1,
                        "grid_point_count": int(row["grid_point_count"]),
                        "max_affected_grid_cells": affected,
                        "sum_affected_cell_hours": affected,
                        "max_freezing_rain_grid_cells": int(row[f"ptype_{HIGH_IMPACT_CODES[0]}_count"]),
                        "max_freezing_rain_on_ground_grid_cells": int(row[f"ptype_{HIGH_IMPACT_CODES[1]}_count"]),
                    }
                else:
                    event["end_time"] = valid
                    event["duration_hours"] = int(str(event["duration_hours"])) + 1
                    event["max_affected_grid_cells"] = max(int(str(event["max_affected_grid_cells"])), affected)
                    event["sum_affected_cell_hours"] = int(str(event["sum_affected_cell_hours"])) + affected
                    event["max_freezing_rain_grid_cells"] = max(
                        int(str(event["max_freezing_rain_grid_cells"])), int(str(row[f"ptype_{HIGH_IMPACT_CODES[0]}_count"]))
                    )
                    event["max_freezing_rain_on_ground_grid_cells"] = max(
                        int(str(event["max_freezing_rain_on_ground_grid_cells"])),
                        int(str(row[f"ptype_{HIGH_IMPACT_CODES[1]}_count"])),
                    )
            else:
                _finalize_event(event, events)
                event = None
    _finalize_event(event, events)
    writer.close()
    os.replace(hourly_partial, hourly_path)
    if first_valid_time is None or last_valid_time is None:
        raise RuntimeError("hourly reduction produced no records")

    event_schema = pa.schema(
        [
            pa.field("event_id", pa.int32(), nullable=False),
            pa.field("start_time", pa.timestamp("s", tz="UTC"), nullable=False),
            pa.field("end_time", pa.timestamp("s", tz="UTC"), nullable=False),
            pa.field("duration_hours", pa.int32(), nullable=False),
            pa.field("grid_point_count", pa.int32(), nullable=False),
            pa.field("max_affected_grid_cells", pa.int32(), nullable=False),
            pa.field("mean_affected_grid_cells", pa.float64(), nullable=False),
            pa.field("sum_affected_cell_hours", pa.int64(), nullable=False),
            pa.field("max_affected_domain_fraction_percent", pa.float64(), nullable=False),
            pa.field("max_freezing_rain_grid_cells", pa.int32(), nullable=False),
            pa.field("max_freezing_rain_on_ground_grid_cells", pa.int32(), nullable=False),
        ],
        metadata={
            b"event_definition": b"contiguous valid hours with code 3 or 13 anywhere in the domain",
            b"area_limitation": b"cell areas unavailable; grid-cell counts and domain fractions are reported",
        },
    )
    events_path = output_root / _safe_relative(final_products["high_impact_events"], label="high impact events")
    _atomic_write_parquet(pa.Table.from_pylist(events, schema=event_schema), events_path)

    map_path = output_root / _safe_relative(final_products["freezing_rain_map"], label="freezing rain map")
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_partial = map_path.with_name(f".{map_path.name}.partial")
    map_partial.unlink(missing_ok=True)
    index_fr = CATEGORY_CODES.index(HIGH_IMPACT_CODES[0])
    index_frg = CATEGORY_CODES.index(HIGH_IMPACT_CODES[1])
    index_dry = CATEGORY_CODES.index(0)
    combined = full_counts[index_fr] + full_counts[index_frg]
    precip_hours = full_hours - full_counts[index_dry]
    with netCDF4.Dataset(map_partial, "w", format="NETCDF4") as dataset_map, netCDF4.Dataset(grid_path) as grid:
        dataset_map.createDimension("cell", npoint)
        dataset_map.setncattr("grid_uuid", grid_uuid)
        dataset_map.setncattr("high_impact_codes", json.dumps(HIGH_IMPACT_CODES))
        for name in ("latitude", "longitude"):
            variable = dataset_map.createVariable(name, "f8", ("cell",), zlib=True, complevel=4, shuffle=True)
            variable[:] = grid.variables[name][:]
            variable.units = grid.variables[name].units
        for name, values in (
            ("freezing_rain_count", full_counts[index_fr]),
            ("freezing_rain_on_ground_count", full_counts[index_frg]),
            ("combined_freezing_rain_count", combined),
            ("precipitation_hour_count", precip_hours),
        ):
            variable = dataset_map.createVariable(name, "u4", ("cell",), zlib=True, complevel=4, shuffle=True)
            variable[:] = values
        all_frequency = dataset_map.createVariable(
            "combined_frequency_all_hours_percent", "f4", ("cell",), zlib=True, complevel=4, shuffle=True
        )
        conditional_frequency = dataset_map.createVariable(
            "combined_frequency_precip_hours_percent", "f4", ("cell",), zlib=True, complevel=4, shuffle=True, fill_value=np.nan
        )
        all_frequency[:] = combined.astype(np.float32) * (100.0 / full_hours)
        conditional_frequency[:] = np.divide(
            combined.astype(np.float32) * 100.0,
            precip_hours,
            out=np.full(npoint, np.nan, dtype=np.float32),
            where=precip_hours > 0,
        )
    os.replace(map_partial, map_path)

    source_size = sum(int(receipt["source_archive"]["size_bytes"]) for receipt in receipts)
    compact_size = sum(int(receipt["compact_archive"]["size_bytes"]) for receipt in receipts)
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "pass" if gap_or_duplicate_count == 0 else "critical",
        "grain": "one categorical field per valid hour and grid cell",
        "hourly_records": hourly_records,
        "first_valid_time": first_valid_time.isoformat(),
        "last_valid_time": last_valid_time.isoformat(),
        "number_of_grid_points": npoint,
        "grid_uuid": grid_uuid,
        "allowed_codes": list(CATEGORY_CODES),
        "category_totals": {str(code): int(category_totals[index]) for index, code in enumerate(CATEGORY_CODES)},
        "categories_absent": [code for index, code in enumerate(CATEGORY_CODES) if category_totals[index] == 0],
        "annual_category_totals": {
            str(year): {str(code): int(values[index]) for index, code in enumerate(CATEGORY_CODES)}
            for year, values in sorted(annual_category_totals.items())
        },
        "annual_category_share_percent": {
            str(year): {
                str(code): 100.0 * int(values[index]) / int(values.sum()) for index, code in enumerate(CATEGORY_CODES)
            }
            for year, values in sorted(annual_category_totals.items())
        },
        "all_no_precip_hours": all_no_precip_hours,
        "temporal_gap_or_duplicate_count": gap_or_duplicate_count,
        "invalid_or_missing_value_count": 0,
        "monthly_tasks": len(receipts),
        "source_size_bytes": source_size,
        "compact_size_bytes": compact_size,
        "compression_ratio": source_size / compact_size,
        "four_bit_exact_decoded_validation": True,
        "constant_field_packing": "GRIB simple packing uses bitsPerValue=0 for constant fields",
        "source_generation_data_quality": _source_data_quality(manifest),
        "high_impact_event_count": len(events),
        "physical_area_limitation": "Cell areas are unavailable in PTYPE GRIB; event extent uses grid-cell counts and domain fraction.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    quality_path = output_root / _safe_relative(final_products["quality_report"], label="quality report")
    _atomic_write_json(report, quality_path)
    markdown_path = output_root / _safe_relative(final_products["quality_report_markdown"], label="quality report markdown")
    _write_quality_markdown(report, markdown_path)
    if report["status"] != "pass":
        raise RuntimeError(f"reduced quality report is critical: {report}")
    products = [hourly_path, frequency_path, events_path, map_path, quality_path, markdown_path, grid_path]
    reduction = {
        "schema_version": REDUCTION_SCHEMA_VERSION,
        "ok": True,
        "status": "complete",
        "manifest_path": str(manifest_path.resolve()),
        "products": {
            path.name: {"path": str(path), "size_bytes": path.stat().st_size, "sha256": _sha256_file(path)} for path in products
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(reduction, output_root / "REDUCTION.json")
    return reduction


def write_analysis_slurm_scripts(
    *,
    manifest_path: Path,
    array_script_path: Path,
    reduce_script_path: Path,
    concurrency: int = 8,
    partition: str = "pp-long",
    wall_time: str = "02:00:00",
    reduce_wall_time: str = "06:00:00",
) -> tuple[Path, Path]:
    manifest = load_analysis_manifest(manifest_path)
    periods = manifest["periods"]
    if not isinstance(periods, list):
        raise RuntimeError("analysis periods are invalid")
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if not partition.startswith("pp-"):
        raise ValueError("analysis jobs require a pp-* partition")
    repo_root = Path(__file__).resolve().parents[2]
    logs = manifest_path.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    quoted_repo = shlex.quote(str(repo_root))
    quoted_manifest = shlex.quote(str(manifest_path.resolve()))
    array_text = f"""#!/usr/bin/env bash
#SBATCH --job-name=ptype-analysis
#SBATCH --partition={partition}
#SBATCH --time={wall_time}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --array=0-{len(periods) - 1}%{concurrency}
#SBATCH --output={logs.resolve()}/%A_%a.out

set -euo pipefail
[ -f /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh
if [[ -n "${{USER_ENV_ROOT:-}}" ]]; then module use "$USER_ENV_ROOT/modules"; fi
cd {quoted_repo}
exec tools/run_balfrin.sh analysis-task {quoted_manifest} "$SLURM_ARRAY_TASK_ID"
"""
    reduce_text = f"""#!/usr/bin/env bash
#SBATCH --job-name=ptype-reduce
#SBATCH --partition={partition}
#SBATCH --time={reduce_wall_time}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --output={logs.resolve()}/reduce-%j.out

set -euo pipefail
[ -f /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh
if [[ -n "${{USER_ENV_ROOT:-}}" ]]; then module use "$USER_ENV_ROOT/modules"; fi
cd {quoted_repo}
exec tools/run_balfrin.sh analysis-reduce {quoted_manifest}
"""
    for path, text in ((array_script_path, array_text), (reduce_script_path, reduce_text)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    return array_script_path, reduce_script_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare compact and analysis-ready products from monthly REA PTYPE archives")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--source-manifest", type=Path, required=True)
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--output-root", type=Path, required=True)
    plan.add_argument("--staging-root", type=Path, required=True)
    plan.add_argument("--start-period", help="first source month to include (YYYYMM)")
    plan.add_argument("--end-period", help="last source month to include (YYYYMM)")
    plan.add_argument("--slurm-script", type=Path)
    plan.add_argument("--reduce-slurm-script", type=Path)
    plan.add_argument("--concurrency", type=int, default=8)
    plan.add_argument("--partition", default="pp-long")
    plan.add_argument("--wall-time", default="02:00:00")
    plan.add_argument("--reduce-wall-time", default="06:00:00")
    task = subparsers.add_parser("run-task")
    task.add_argument("--manifest", type=Path, required=True)
    task.add_argument("--index", type=int)
    task.add_argument("--lock-timeout-s", type=float, default=0.0)
    reduce_parser = subparsers.add_parser("reduce")
    reduce_parser.add_argument("--manifest", type=Path, required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--manifest", type=Path, required=True)
    status.add_argument("--verify-outputs", action="store_true")
    retire = subparsers.add_parser("retire-source")
    retire.add_argument("--manifest", type=Path, required=True)
    retire.add_argument("--confirm-source-root", type=Path, required=True)
    retire.add_argument(
        "--delete-source",
        action="store_true",
        help="permanently unlink only the exact source files sealed by the promotion contract",
    )
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args()
    if args.command == "plan":
        manifest = build_analysis_manifest(
            source_manifest_path=args.source_manifest,
            manifest_path=args.manifest,
            output_root=args.output_root,
            staging_root=args.staging_root,
            start_period=args.start_period,
            end_period=args.end_period,
        )
        array_script = args.slurm_script or args.manifest.with_suffix(".sbatch")
        reduce_script = args.reduce_slurm_script or args.manifest.with_suffix(".reduce.sbatch")
        write_analysis_slurm_scripts(
            manifest_path=args.manifest,
            array_script_path=array_script,
            reduce_script_path=reduce_script,
            concurrency=args.concurrency,
            partition=args.partition,
            wall_time=args.wall_time,
            reduce_wall_time=args.reduce_wall_time,
        )
        print(json.dumps({"manifest": manifest, "slurm_script": str(array_script), "reduce_slurm_script": str(reduce_script)}, indent=2))
        return 0
    if args.command == "run-task":
        raw_index = args.index if args.index is not None else os.environ.get("SLURM_ARRAY_TASK_ID")
        if raw_index is None:
            raise RuntimeError("run-task requires --index or SLURM_ARRAY_TASK_ID")
        receipt = run_analysis_task(manifest_path=args.manifest, index=int(raw_index), lock_timeout_s=args.lock_timeout_s)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    if args.command == "reduce":
        reduction = reduce_analysis(manifest_path=args.manifest)
        print(json.dumps(reduction, indent=2, sort_keys=True))
        return 0
    if args.command == "retire-source":
        result = promote_compact_archive(
            manifest_path=args.manifest,
            confirmed_source_root=args.confirm_source_root,
            delete_source=args.delete_source,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    status = analysis_status(manifest_path=args.manifest, verify_outputs=args.verify_outputs)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
