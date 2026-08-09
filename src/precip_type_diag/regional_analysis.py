"""Regional hourly summaries and event catalogues from a compact REA PTYPE archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import eccodes
import netCDF4
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .analysis import (
    CATEGORY_CODES,
    CATEGORY_NAMES,
    HIGH_IMPACT_CODES,
    _atomic_write_parquet,
    _categorical_values,
    _expected_messages,
    _hourly_schema,
    _message_metadata,
    _receipt_path,
    _safe_relative,
    _sha256_file,
    _validate_metadata,
    _write_immutable_json,
    analysis_status,
    load_analysis_manifest,
)
from .constants import PTYPE_BITS_PER_VALUE, PrecipitationTypeCode
from .gribio import bootstrap_eccodes_definitions
from .operational import CycleLock, _atomic_write_json
from .provenance import collect_runtime_provenance

REGIONAL_MANIFEST_SCHEMA_VERSION = 1
REGIONAL_MANIFEST_MODE = "rea_l_ch1_compact_regional_analysis"
REGION_MASK_SCHEMA_VERSION = 1
REGIONAL_CONTRACT_SCHEMA_VERSION = 1
REGIONAL_REDUCTION_SCHEMA_VERSION = 1

FREEZING_DRIZZLE_CODE = int(PrecipitationTypeCode.FREEZING_DRIZZLE)
ICY_LIQUID_CODES = (*HIGH_IMPACT_CODES, FREEZING_DRIZZLE_CODE)
EVENT_COMPONENT_CODES = (
    int(PrecipitationTypeCode.FREEZING_RAIN),
    FREEZING_DRIZZLE_CODE,
    int(PrecipitationTypeCode.FREEZING_RAIN_ON_GROUND),
)
EVENT_DEFINITIONS = {
    "high_impact": HIGH_IMPACT_CODES,
    "freezing_drizzle": (FREEZING_DRIZZLE_CODE,),
    "icy_liquid": ICY_LIQUID_CODES,
}


def _region_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        raise ValueError("region_id must contain only lowercase letters, digits, underscores, or hyphens")
    return value


def _selected_geojson_geometries(
    payload: object,
    *,
    feature_property: str | None,
    feature_value: str | None,
) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        raise RuntimeError("GeoJSON root must be an object")
    if payload.get("type") == "FeatureCollection":
        raw_features = payload.get("features")
        if not isinstance(raw_features, list):
            raise RuntimeError("GeoJSON FeatureCollection has no features")
        features = raw_features
    elif payload.get("type") == "Feature":
        features = [payload]
    else:
        features = [{"type": "Feature", "properties": {}, "geometry": payload}]
    geometries: list[dict[str, object]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if feature_property is not None:
            if not isinstance(properties, dict) or str(properties.get(feature_property)) != feature_value:
                continue
        geometry = feature.get("geometry")
        if isinstance(geometry, dict):
            geometries.append(geometry)
    if not geometries:
        selector = f" {feature_property}={feature_value!r}" if feature_property is not None else ""
        raise RuntimeError(f"GeoJSON contains no selected geometries{selector}")
    return geometries


def _points_in_ring(longitude: np.ndarray, latitude: np.ndarray, ring: object) -> np.ndarray:
    coordinates = np.asarray(ring, dtype=np.float64)
    if coordinates.ndim != 2 or coordinates.shape[1] < 2 or coordinates.shape[0] < 4:
        raise RuntimeError("GeoJSON polygon ring must contain at least four longitude/latitude positions")
    x = coordinates[:, 0]
    y = coordinates[:, 1]
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise RuntimeError("GeoJSON polygon coordinates must be finite")
    candidate = (
        (longitude >= float(x.min()))
        & (longitude <= float(x.max()))
        & (latitude >= float(y.min()))
        & (latitude <= float(y.max()))
    )
    selected = np.flatnonzero(candidate)
    inside = np.zeros(longitude.size, dtype=bool)
    if selected.size == 0:
        return inside
    px = longitude[selected]
    py = latitude[selected]
    local = np.zeros(selected.size, dtype=bool)
    j = x.size - 1
    for i in range(x.size):
        crosses = (y[i] > py) != (y[j] > py)
        intersection = (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i] + np.finfo(np.float64).tiny) + x[i]
        local ^= crosses & (px < intersection)
        j = i
    inside[selected] = local
    return inside


def _geometry_mask(longitude: np.ndarray, latitude: np.ndarray, geometry: dict[str, object]) -> np.ndarray:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        polygons = [coordinates]
    elif geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list):
            raise RuntimeError("GeoJSON geometry coordinates are invalid")
        polygons = coordinates
    else:
        raise RuntimeError(f"unsupported GeoJSON geometry type {geometry_type!r}; expected Polygon or MultiPolygon")
    if not isinstance(polygons, list):
        raise RuntimeError("GeoJSON geometry coordinates are invalid")
    result = np.zeros(longitude.size, dtype=bool)
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise RuntimeError("GeoJSON polygon has no exterior ring")
        polygon_mask = _points_in_ring(longitude, latitude, polygon[0])
        for hole in polygon[1:]:
            polygon_mask &= ~_points_in_ring(longitude, latitude, hole)
        result |= polygon_mask
    return result


def build_region_mask(
    *,
    grid_path: Path,
    geojson_path: Path,
    output_path: Path,
    region_name: str,
    boundary_source: str,
    feature_property: str | None = None,
    feature_value: str | None = None,
) -> dict[str, object]:
    if (feature_property is None) != (feature_value is None):
        raise ValueError("feature_property and feature_value must be provided together")
    grid_path = grid_path.resolve()
    geojson_path = geojson_path.resolve()
    if not grid_path.is_file() or not geojson_path.is_file():
        raise FileNotFoundError("grid and GeoJSON boundary files must exist")
    grid_sha256 = _sha256_file(grid_path)
    boundary_sha256 = _sha256_file(geojson_path)
    with netCDF4.Dataset(grid_path) as grid:
        longitude = np.asarray(grid.variables["longitude"][:], dtype=np.float64).reshape(-1)
        latitude = np.asarray(grid.variables["latitude"][:], dtype=np.float64).reshape(-1)
        grid_uuid = str(grid.getncattr("grid_uuid"))
    if longitude.shape != latitude.shape or not np.all(np.isfinite(longitude)) or not np.all(np.isfinite(latitude)):
        raise RuntimeError("grid longitude/latitude coordinates are invalid")
    payload = json.loads(geojson_path.read_text(encoding="utf-8"))
    geometries = _selected_geojson_geometries(
        payload,
        feature_property=feature_property,
        feature_value=feature_value,
    )
    mask = np.zeros(longitude.size, dtype=bool)
    for geometry in geometries:
        mask |= _geometry_mask(longitude, latitude, geometry)
    selected_count = int(np.count_nonzero(mask))
    if selected_count == 0 or selected_count == mask.size:
        raise RuntimeError(f"region mask selects {selected_count} of {mask.size} cells; expected a non-empty strict subset")
    contract: dict[str, object] = {
        "schema_version": REGION_MASK_SCHEMA_VERSION,
        "region_name": region_name,
        "grid_path": str(grid_path),
        "grid_sha256": grid_sha256,
        "grid_uuid": grid_uuid,
        "grid_point_count": int(mask.size),
        "selected_grid_point_count": selected_count,
        "boundary_path": str(geojson_path),
        "boundary_sha256": boundary_sha256,
        "boundary_source": boundary_source,
        "feature_property": feature_property or "",
        "feature_value": feature_value or "",
        "mask_algorithm": "longitude/latitude point-in-polygon ray casting; polygon holes excluded",
    }
    if output_path.is_file():
        existing, _ = _load_region_mask(output_path)
        comparable = {key: existing[key] for key in contract}
        if comparable != contract:
            raise RuntimeError(f"immutable region-mask contract mismatch at {output_path}")
        return existing
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp.nc", dir=output_path.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        with netCDF4.Dataset(temporary_path, "w", format="NETCDF4") as dataset:
            dataset.createDimension("cell", mask.size)
            for key, value in contract.items():
                dataset.setncattr(key, value)
            variable = dataset.createVariable("region_mask", "u1", ("cell",), zlib=True, complevel=4, shuffle=True)
            variable.setncattr("flag_values", np.asarray([0, 1], dtype=np.uint8))
            variable.setncattr("flag_meanings", "outside_region inside_region")
            variable[:] = mask.astype(np.uint8)
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    result, _ = _load_region_mask(output_path)
    return result


def _load_region_mask(path: Path) -> tuple[dict[str, object], np.ndarray]:
    with netCDF4.Dataset(path) as dataset:
        if int(dataset.getncattr("schema_version")) != REGION_MASK_SCHEMA_VERSION:
            raise RuntimeError(f"unsupported region-mask schema at {path}")
        raw = np.asarray(dataset.variables["region_mask"][:], dtype=np.uint8).reshape(-1)
        if not np.all(np.isin(raw, np.asarray([0, 1], dtype=np.uint8))):
            raise RuntimeError(f"region mask contains values other than zero and one: {path}")
        mask = raw.astype(bool)
        contract = {
            "schema_version": int(dataset.getncattr("schema_version")),
            "region_name": str(dataset.getncattr("region_name")),
            "grid_path": str(dataset.getncattr("grid_path")),
            "grid_sha256": str(dataset.getncattr("grid_sha256")),
            "grid_uuid": str(dataset.getncattr("grid_uuid")),
            "grid_point_count": int(dataset.getncattr("grid_point_count")),
            "selected_grid_point_count": int(dataset.getncattr("selected_grid_point_count")),
            "boundary_path": str(dataset.getncattr("boundary_path")),
            "boundary_sha256": str(dataset.getncattr("boundary_sha256")),
            "boundary_source": str(dataset.getncattr("boundary_source")),
            "feature_property": str(dataset.getncattr("feature_property")),
            "feature_value": str(dataset.getncattr("feature_value")),
            "mask_algorithm": str(dataset.getncattr("mask_algorithm")),
        }
    if mask.size != contract["grid_point_count"] or int(mask.sum()) != contract["selected_grid_point_count"]:
        raise RuntimeError(f"region-mask counts disagree with its contract: {path}")
    return contract, mask


def _regional_hourly_schema(region_name: str, mask_sha256: str) -> pa.Schema:
    fields = [
        pa.field("valid_time", pa.timestamp("s", tz="UTC"), nullable=False),
        pa.field("cycle_date", pa.int32(), nullable=False),
        pa.field("step", pa.int8(), nullable=False),
        pa.field("region_grid_point_count", pa.int32(), nullable=False),
        pa.field("precip_grid_point_count", pa.int32(), nullable=False),
        pa.field("high_impact_grid_point_count", pa.int32(), nullable=False),
        pa.field("freezing_drizzle_grid_point_count", pa.int32(), nullable=False),
        pa.field("icy_liquid_grid_point_count", pa.int32(), nullable=False),
        *(pa.field(f"ptype_{code}_count", pa.int32(), nullable=False) for code in CATEGORY_CODES),
        pa.field("compact_bits_per_value", pa.int8(), nullable=False),
        pa.field("full_domain_decoded_sha256", pa.string(), nullable=False),
        pa.field("regional_decoded_sha256", pa.string(), nullable=False),
        pa.field("all_no_precip", pa.bool_(), nullable=False),
        pa.field("qc_ok", pa.bool_(), nullable=False),
    ]
    return pa.schema(
        fields,
        metadata={
            b"grain": b"one row per valid hour",
            b"timezone": b"UTC",
            b"region_name": region_name.encode(),
            b"region_mask_sha256": mask_sha256.encode(),
        },
    )


def _period_paths(period: dict[str, object], region_id: str) -> dict[str, str]:
    name = str(period["period"])
    return {"regional_hourly_counts": str(Path("monthly") / f"ptype_hourly_counts_{region_id}_{name}.parquet")}


def build_regional_manifest(
    *,
    analysis_manifest_path: Path,
    region_mask_path: Path,
    manifest_path: Path,
    output_root: Path,
    region_id: str,
    start_period: str | None = None,
    end_period: str | None = None,
) -> dict[str, object]:
    region_id = _region_id(region_id)
    analysis_manifest_path = analysis_manifest_path.resolve()
    region_mask_path = region_mask_path.resolve()
    output_root = output_root.resolve()
    source = load_analysis_manifest(analysis_manifest_path)
    source_status = analysis_status(manifest_path=analysis_manifest_path, verify_outputs=False)
    if source_status.get("complete") is not True:
        raise RuntimeError("compact archive analysis must be complete before regional planning")
    source_output = Path(str(source["output_root"])).resolve()
    if output_root == source_output or output_root.is_relative_to(source_output) or source_output.is_relative_to(output_root):
        raise ValueError("regional output_root and canonical analysis output_root must not overlap")
    mask_contract, _ = _load_region_mask(region_mask_path)
    grid_path = source_output / _safe_relative(cast(dict[str, object], source["final_products"])["grid"], label="grid")
    if mask_contract["grid_sha256"] != _sha256_file(grid_path):
        raise RuntimeError("region mask was not built from the canonical analysis grid")
    raw_periods = source.get("periods")
    if not isinstance(raw_periods, list):
        raise RuntimeError("analysis manifest periods are invalid")
    lower = start_period
    upper = end_period
    for label, value in (("start_period", lower), ("end_period", upper)):
        if value is not None and not re.fullmatch(r"\d{6}", value):
            raise ValueError(f"{label} must use YYYYMM")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("start_period is after end_period")
    periods: list[dict[str, object]] = []
    for source_index, raw in enumerate(raw_periods):
        if not isinstance(raw, dict):
            raise RuntimeError("analysis period is invalid")
        name = str(raw["period"])
        if (lower is not None and name < lower) or (upper is not None and name > upper):
            continue
        periods.append(
            {
                "index": len(periods),
                "source_index": source_index,
                "period": name,
                "dates": list(cast(list[object], raw["dates"])),
                "message_count": int(str(raw["message_count"])),
                "compact_archive": str(_safe_relative(raw["compact_archive"], label="compact archive")),
                "reference_hourly_counts": str(_safe_relative(raw["hourly_counts"], label="reference hourly counts")),
                **_period_paths(raw, region_id),
            }
        )
    if not periods:
        raise ValueError("no compact archive periods match the requested range")
    final_products = {
        "hourly_counts": f"ptype_hourly_counts_{region_id}.parquet",
        "high_impact_events": f"high_impact_events_{region_id}.parquet",
        "freezing_drizzle_events": f"freezing_drizzle_events_{region_id}.parquet",
        "icy_liquid_events": f"icy_liquid_events_{region_id}.parquet",
        "quality_report": "REGIONAL_DATA_QUALITY_REPORT.json",
        "quality_report_markdown": "REGIONAL_DATA_QUALITY_REPORT.md",
        "reduction_receipt": "REGIONAL_REDUCTION.json",
    }
    manifest: dict[str, object] = {
        "schema_version": REGIONAL_MANIFEST_SCHEMA_VERSION,
        "mode": REGIONAL_MANIFEST_MODE,
        "analysis_manifest_path": str(analysis_manifest_path),
        "analysis_manifest_sha256": _sha256_file(analysis_manifest_path),
        "analysis_output_root": str(source_output),
        "grid_path": str(grid_path),
        "grid_sha256": _sha256_file(grid_path),
        "region_mask_path": str(region_mask_path),
        "region_mask_sha256": _sha256_file(region_mask_path),
        "region_mask_contract": mask_contract,
        "region_id": region_id,
        "output_root": str(output_root),
        "manifest_path": str(manifest_path.resolve()),
        "selected_period_range": {"start": periods[0]["period"], "end": periods[-1]["period"]},
        "category_codes": list(CATEGORY_CODES),
        "category_names": list(CATEGORY_NAMES),
        "event_definitions": {name: list(codes) for name, codes in EVENT_DEFINITIONS.items()},
        "periods": periods,
        "final_products": final_products,
    }
    if manifest_path.is_file():
        existing = load_regional_manifest(manifest_path)
        if existing != manifest:
            raise RuntimeError(f"immutable regional manifest mismatch at {manifest_path}; use a new campaign")
        return existing
    _atomic_write_json(manifest, manifest_path)
    return manifest


def load_regional_manifest(path: Path) -> dict[str, object]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != REGIONAL_MANIFEST_SCHEMA_VERSION or manifest.get("mode") != REGIONAL_MANIFEST_MODE:
        raise RuntimeError(f"unsupported regional analysis manifest: {path}")
    checks = (
        (Path(str(manifest["analysis_manifest_path"])), str(manifest["analysis_manifest_sha256"]), "analysis manifest"),
        (Path(str(manifest["grid_path"])), str(manifest["grid_sha256"]), "grid"),
        (Path(str(manifest["region_mask_path"])), str(manifest["region_mask_sha256"]), "region mask"),
    )
    for input_path, expected, label in checks:
        if not input_path.is_file() or _sha256_file(input_path) != expected:
            raise RuntimeError(f"{label} changed or is unavailable: {input_path}")
    periods = manifest.get("periods")
    if not isinstance(periods, list) or not periods:
        raise RuntimeError("regional analysis manifest has no periods")
    for index, period in enumerate(periods):
        if not isinstance(period, dict) or int(str(period.get("index"))) != index:
            raise RuntimeError(f"invalid regional analysis period at index {index}")
    return manifest


def _regional_period(manifest: dict[str, object], index: int) -> dict[str, object]:
    periods = cast(list[object], manifest["periods"])
    if not 0 <= index < len(periods) or not isinstance(periods[index], dict):
        raise IndexError(f"regional analysis index {index} is outside 0..{len(periods) - 1}")
    return cast(dict[str, object], periods[index])


def _regional_output(manifest: dict[str, object], period: dict[str, object]) -> Path:
    return Path(str(manifest["output_root"])) / _safe_relative(period["regional_hourly_counts"], label="regional hourly counts")


def _regional_receipt_path(manifest_path: Path, period: dict[str, object]) -> Path:
    return manifest_path.parent / "receipts" / f"{int(str(period['index'])):05d}-{period['period']}.json"


def _ensure_regional_contract(manifest_path: Path, manifest: dict[str, object]) -> None:
    contract = {
        "schema_version": REGIONAL_CONTRACT_SCHEMA_VERSION,
        "mode": REGIONAL_MANIFEST_MODE,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": _sha256_file(manifest_path),
        "analysis_manifest_path": manifest["analysis_manifest_path"],
        "analysis_manifest_sha256": manifest["analysis_manifest_sha256"],
        "region_mask_path": manifest["region_mask_path"],
        "region_mask_sha256": manifest["region_mask_sha256"],
        "region_mask_contract": manifest["region_mask_contract"],
        "event_definitions": manifest["event_definitions"],
        "event_extent_contract": "grid-cell counts and region fractions; physical area is unavailable",
    }
    _write_immutable_json(contract, Path(str(manifest["output_root"])) / "REGIONAL_ANALYSIS_CONTRACT.json")


def _canonical_period(manifest: dict[str, object], period: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    analysis_manifest_path = Path(str(manifest["analysis_manifest_path"]))
    source = load_analysis_manifest(analysis_manifest_path)
    source_periods = cast(list[object], source["periods"])
    source_index = int(str(period["source_index"]))
    if not 0 <= source_index < len(source_periods) or not isinstance(source_periods[source_index], dict):
        raise RuntimeError("regional period source index is invalid")
    source_period = cast(dict[str, object], source_periods[source_index])
    if str(source_period["period"]) != str(period["period"]):
        raise RuntimeError("regional and canonical period names disagree")
    receipt = json.loads(_receipt_path(analysis_manifest_path, source_period).read_text(encoding="utf-8"))
    if receipt.get("ok") is not True:
        raise RuntimeError(f"canonical compact receipt is not complete for {period['period']}")
    return source_period, receipt


def _regional_receipt_complete(
    *, manifest_path: Path, manifest: dict[str, object], period: dict[str, object], verify_outputs: bool
) -> bool:
    path = _regional_receipt_path(manifest_path, period)
    output = _regional_output(manifest, period)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("ok") is not True or not output.is_file():
            return False
        output_evidence = cast(dict[str, object], receipt["regional_hourly_counts"])
        if int(str(output_evidence["size_bytes"])) != output.stat().st_size:
            return False
        compact = Path(str(manifest["analysis_output_root"])) / _safe_relative(period["compact_archive"], label="compact archive")
        compact_evidence = cast(dict[str, object], receipt["compact_archive"])
        if not compact.is_file() or int(str(compact_evidence["size_bytes"])) != compact.stat().st_size:
            return False
        if verify_outputs and (
            str(output_evidence["sha256"]) != _sha256_file(output)
            or str(compact_evidence["sha256"]) != _sha256_file(compact)
        ):
            return False
    except Exception:
        return False
    return True


def _utc(value: object) -> datetime:
    result = cast(datetime, value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def run_regional_task(*, manifest_path: Path, index: int, lock_timeout_s: float = 0.0) -> dict[str, object]:
    bootstrap_eccodes_definitions()
    manifest = load_regional_manifest(manifest_path)
    period = _regional_period(manifest, index)
    receipt_path = _regional_receipt_path(manifest_path, period)
    output_path = _regional_output(manifest, period)
    lock = CycleLock(manifest_path.parent / "locks" / f"{index:05d}-{period['period']}.lock", timeout_s=lock_timeout_s)
    lock.acquire()
    started = datetime.now(timezone.utc)
    try:
        _ensure_regional_contract(manifest_path, manifest)
        if _regional_receipt_complete(
            manifest_path=manifest_path,
            manifest=manifest,
            period=period,
            verify_outputs=True,
        ):
            return json.loads(receipt_path.read_text(encoding="utf-8"))
        source_period, canonical_receipt = _canonical_period(manifest, period)
        compact_path = Path(str(manifest["analysis_output_root"])) / _safe_relative(
            period["compact_archive"], label="compact archive"
        )
        reference_path = Path(str(manifest["analysis_output_root"])) / _safe_relative(
            period["reference_hourly_counts"], label="reference hourly counts"
        )
        if not compact_path.is_file() or not reference_path.is_file():
            raise RuntimeError(f"canonical compact inputs are missing for {period['period']}")
        mask_contract, mask = _load_region_mask(Path(str(manifest["region_mask_path"])))
        region_count = int(mask.sum())
        reference = pq.read_table(reference_path, schema=_hourly_schema()).to_pylist()
        expected = _expected_messages(source_period)
        if len(reference) != len(expected):
            raise RuntimeError("reference hourly Parquet row count does not match compact manifest")
        rows: list[dict[str, object]] = []
        compact_sha256 = hashlib.sha256()
        full_decoded_sha256 = hashlib.sha256()
        regional_decoded_sha256 = hashlib.sha256()
        category_totals = np.zeros(len(CATEGORY_CODES), dtype=np.uint64)
        position = 0
        with compact_path.open("rb") as handle:
            while True:
                message_id = eccodes.codes_grib_new_from_file(handle)
                if message_id is None:
                    break
                try:
                    if position >= len(expected):
                        raise RuntimeError("compact archive has more messages than its manifest")
                    date, step = expected[position]
                    metadata = _message_metadata(message_id)
                    valid = _validate_metadata(metadata, date, step, position=position + 1, path=compact_path)
                    categorical = _categorical_values(message_id, position=position + 1, path=compact_path)
                    if categorical.size != mask.size or str(metadata.get("uuidOfHGrid") or "") != mask_contract["grid_uuid"]:
                        raise RuntimeError(f"compact archive grid disagrees with region mask at message {position + 1}")
                    bits = int(str(metadata["bitsPerValue"]))
                    expected_bits = 0 if bool(np.all(categorical == categorical[0])) else PTYPE_BITS_PER_VALUE
                    if bits != expected_bits:
                        raise RuntimeError(f"compact packing mismatch at message {position + 1}: {bits} != {expected_bits}")
                    raw = eccodes.codes_get_message(message_id)
                    compact_sha256.update(raw)
                    message_digest = hashlib.sha256(categorical.tobytes()).hexdigest()
                    full_decoded_sha256.update(categorical.tobytes())
                    canonical = reference[position]
                    if (
                        _utc(canonical["valid_time"]) != valid
                        or int(canonical["cycle_date"]) != int(date)
                        or int(canonical["step"]) != step
                        or int(canonical["grid_point_count"]) != categorical.size
                        or str(canonical["decoded_sha256"]) != message_digest
                    ):
                        raise RuntimeError(f"canonical hourly evidence mismatch at message {position + 1}")
                    domain_counts = np.bincount(categorical, minlength=256)[np.asarray(CATEGORY_CODES)]
                    if any(int(canonical[f"ptype_{code}_count"]) != int(domain_counts[i]) for i, code in enumerate(CATEGORY_CODES)):
                        raise RuntimeError(f"canonical domain counts mismatch at message {position + 1}")
                    selected = categorical[mask]
                    regional_decoded_sha256.update(selected.tobytes())
                    counts = np.bincount(selected, minlength=256)[np.asarray(CATEGORY_CODES)]
                    if int(counts.sum()) != region_count:
                        raise RuntimeError(f"regional category partition failed at message {position + 1}")
                    category_totals += counts.astype(np.uint64)
                    by_code = {code: int(counts[i]) for i, code in enumerate(CATEGORY_CODES)}
                    precip_count = region_count - by_code[0]
                    row: dict[str, object] = {
                        "valid_time": valid,
                        "cycle_date": int(date),
                        "step": step,
                        "region_grid_point_count": region_count,
                        "precip_grid_point_count": precip_count,
                        "high_impact_grid_point_count": sum(by_code[code] for code in HIGH_IMPACT_CODES),
                        "freezing_drizzle_grid_point_count": by_code[FREEZING_DRIZZLE_CODE],
                        "icy_liquid_grid_point_count": sum(by_code[code] for code in ICY_LIQUID_CODES),
                        "compact_bits_per_value": bits,
                        "full_domain_decoded_sha256": message_digest,
                        "regional_decoded_sha256": hashlib.sha256(selected.tobytes()).hexdigest(),
                        "all_no_precip": precip_count == 0,
                        "qc_ok": True,
                    }
                    row.update({f"ptype_{code}_count": by_code[code] for code in CATEGORY_CODES})
                    rows.append(row)
                    position += 1
                finally:
                    eccodes.codes_release(message_id)
        if position != len(expected):
            raise RuntimeError(f"compact archive has {position} messages; expected {len(expected)}")
        canonical_compact = cast(dict[str, object], canonical_receipt["compact_archive"])
        if compact_sha256.hexdigest() != canonical_compact.get("sha256"):
            raise RuntimeError("compact byte checksum disagrees with the sealed canonical receipt")
        if full_decoded_sha256.hexdigest() != canonical_receipt.get("decoded_sha256"):
            raise RuntimeError("compact decoded checksum disagrees with the sealed canonical receipt")
        schema = _regional_hourly_schema(str(mask_contract["region_name"]), str(manifest["region_mask_sha256"]))
        _atomic_write_parquet(pa.Table.from_pylist(rows, schema=schema), output_path)
        finished = datetime.now(timezone.utc)
        receipt: dict[str, object] = {
            "schema_version": 1,
            "index": index,
            "period": period["period"],
            "status": "complete",
            "ok": True,
            "message_count": position,
            "region_name": mask_contract["region_name"],
            "region_grid_point_count": region_count,
            "region_mask_sha256": manifest["region_mask_sha256"],
            "compact_archive": {
                "path": str(compact_path),
                "size_bytes": compact_path.stat().st_size,
                "sha256": compact_sha256.hexdigest(),
                "decoded_sha256": full_decoded_sha256.hexdigest(),
                "canonical_receipt_match": True,
            },
            "reference_hourly_counts": {
                "path": str(reference_path),
                "size_bytes": reference_path.stat().st_size,
                "sha256": _sha256_file(reference_path),
                "rows": len(reference),
                "all_counts_and_message_checksums_match": True,
            },
            "regional_hourly_counts": {
                "path": str(output_path),
                "size_bytes": output_path.stat().st_size,
                "sha256": _sha256_file(output_path),
                "rows": len(rows),
                "decoded_sha256": regional_decoded_sha256.hexdigest(),
            },
            "category_totals": {str(code): int(category_totals[i]) for i, code in enumerate(CATEGORY_CODES)},
            "provenance": collect_runtime_provenance(),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "wall_s": (finished - started).total_seconds(),
        }
        _atomic_write_json(receipt, receipt_path)
        return receipt
    except Exception as exc:
        failure = {
            "schema_version": 1,
            "index": index,
            "period": period["period"],
            "status": "critical",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(failure, receipt_path)
        raise
    finally:
        lock.release()


def regional_status(*, manifest_path: Path, verify_outputs: bool = False) -> dict[str, object]:
    manifest = load_regional_manifest(manifest_path)
    periods = cast(list[object], manifest["periods"])
    complete: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    for raw in periods:
        period = cast(dict[str, object], raw)
        receipt_path = _regional_receipt_path(manifest_path, period)
        try:
            status = json.loads(receipt_path.read_text(encoding="utf-8")).get("status")
        except Exception:
            status = None
        if status == "critical":
            failed.append(str(period["period"]))
        elif _regional_receipt_complete(
            manifest_path=manifest_path,
            manifest=manifest,
            period=period,
            verify_outputs=verify_outputs,
        ):
            complete.append(str(period["period"]))
        else:
            pending.append(str(period["period"]))
    reduction_path = Path(str(manifest["output_root"])) / "REGIONAL_REDUCTION.json"
    reduction_complete = False
    if reduction_path.is_file():
        try:
            reduction = json.loads(reduction_path.read_text(encoding="utf-8"))
            reduction_complete = reduction.get("ok") is True
            if reduction_complete and verify_outputs:
                for evidence in cast(dict[str, object], reduction["products"]).values():
                    item = cast(dict[str, object], evidence)
                    path = Path(str(item["path"]))
                    if not path.is_file() or path.stat().st_size != int(str(item["size_bytes"])) or _sha256_file(path) != item["sha256"]:
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
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(result, manifest_path.parent / "regional-analysis-status.json")
    return result


def _event_schema(*, name: str, codes: tuple[int, ...], region_name: str, region_count: int) -> pa.Schema:
    return pa.schema(
        [
            pa.field("event_id", pa.int32(), nullable=False),
            pa.field("start_time", pa.timestamp("s", tz="UTC"), nullable=False),
            pa.field("end_time", pa.timestamp("s", tz="UTC"), nullable=False),
            pa.field("peak_time", pa.timestamp("s", tz="UTC"), nullable=False),
            pa.field("duration_hours", pa.int32(), nullable=False),
            pa.field("region_grid_point_count", pa.int32(), nullable=False),
            pa.field("max_affected_grid_cells", pa.int32(), nullable=False),
            pa.field("mean_affected_grid_cells", pa.float64(), nullable=False),
            pa.field("sum_affected_cell_hours", pa.int64(), nullable=False),
            pa.field("max_affected_region_fraction_percent", pa.float64(), nullable=False),
            pa.field("max_precip_grid_cells", pa.int32(), nullable=False),
            *(pa.field(f"max_ptype_{code}_grid_cells", pa.int32(), nullable=False) for code in EVENT_COMPONENT_CODES),
        ],
        metadata={
            b"event_name": name.encode(),
            b"event_codes": json.dumps(codes).encode(),
            b"event_definition": f"contiguous valid hours with any of codes {codes} in {region_name}".encode(),
            b"region_grid_point_count": str(region_count).encode(),
            b"area_limitation": b"cell areas unavailable; grid-cell counts and region fractions are reported",
        },
    )


def _finish_event(event: dict[str, object] | None, events: list[dict[str, object]]) -> None:
    if event is None:
        return
    duration = int(str(event["duration_hours"]))
    region_count = int(str(event["region_grid_point_count"]))
    maximum = int(str(event["max_affected_grid_cells"]))
    event["mean_affected_grid_cells"] = int(str(event["sum_affected_cell_hours"])) / duration
    event["max_affected_region_fraction_percent"] = 100.0 * maximum / region_count
    events.append(event)


def _event_catalogues(rows: list[dict[str, object]], *, region_name: str, region_count: int) -> dict[str, pa.Table]:
    result: dict[str, pa.Table] = {}
    for name, codes in EVENT_DEFINITIONS.items():
        events: list[dict[str, object]] = []
        event: dict[str, object] | None = None
        for row in rows:
            valid = _utc(row["valid_time"])
            affected = sum(int(str(row[f"ptype_{code}_count"])) for code in codes)
            if affected == 0:
                _finish_event(event, events)
                event = None
                continue
            contiguous = event is not None and valid == _utc(event["end_time"]) + timedelta(hours=1)
            if not contiguous:
                _finish_event(event, events)
                event = {
                    "event_id": len(events) + 1,
                    "start_time": valid,
                    "end_time": valid,
                    "peak_time": valid,
                    "duration_hours": 1,
                    "region_grid_point_count": region_count,
                    "max_affected_grid_cells": affected,
                    "sum_affected_cell_hours": affected,
                    "max_precip_grid_cells": int(str(row["precip_grid_point_count"])),
                    **{
                        f"max_ptype_{code}_grid_cells": int(str(row[f"ptype_{code}_count"]))
                        for code in EVENT_COMPONENT_CODES
                    },
                }
            else:
                if event is None:
                    raise RuntimeError("invalid event state")
                event["end_time"] = valid
                event["duration_hours"] = int(str(event["duration_hours"])) + 1
                event["sum_affected_cell_hours"] = int(str(event["sum_affected_cell_hours"])) + affected
                if affected > int(str(event["max_affected_grid_cells"])):
                    event["max_affected_grid_cells"] = affected
                    event["peak_time"] = valid
                event["max_precip_grid_cells"] = max(
                    int(str(event["max_precip_grid_cells"])), int(str(row["precip_grid_point_count"]))
                )
                for code in EVENT_COMPONENT_CODES:
                    key = f"max_ptype_{code}_grid_cells"
                    event[key] = max(int(str(event[key])), int(str(row[f"ptype_{code}_count"])))
        _finish_event(event, events)
        result[name] = pa.Table.from_pylist(
            events,
            schema=_event_schema(name=name, codes=codes, region_name=region_name, region_count=region_count),
        )
    return result


def _write_quality_markdown(report: dict[str, object], path: Path) -> None:
    event_counts = cast(dict[str, int], report["event_counts"])
    lines = [
        f"# {report['region_name']} regional PTYPE data-quality report",
        "",
        f"- Status: **{report['status']}**",
        f"- Valid hours: {report['hourly_records']:,} (`{report['first_valid_time']}` through `{report['last_valid_time']}`)",
        f"- Selected grid cells: {report['region_grid_point_count']:,}",
        f"- Temporal gaps or duplicates: {report['temporal_gap_or_duplicate_count']}",
        f"- Monthly compact/archive checksum mismatches: {report['monthly_checksum_mismatch_count']}",
        f"- Regional category-partition failures: {report['regional_partition_failure_count']}",
        "",
        "## Event catalogues",
        "",
        f"- Freezing rain / freezing rain on ground (codes 3, 13): {event_counts['high_impact']:,}",
        f"- Freezing drizzle (code 12): {event_counts['freezing_drizzle']:,}",
        f"- Combined icy liquid (codes 3, 12, 13): {event_counts['icy_liquid']:,}",
        "",
        "Events are contiguous valid hours with at least one affected selected cell. Filter by maximum affected-cell count or "
        "maximum region fraction for severity screening. Do not interpret grid-cell counts as physical area without a reviewed cell-area field.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def reduce_regional_analysis(*, manifest_path: Path) -> dict[str, object]:
    manifest = load_regional_manifest(manifest_path)
    status = regional_status(manifest_path=manifest_path, verify_outputs=False)
    if status["tasks_complete"] is not True:
        raise RuntimeError("all monthly regional tasks must complete before reduction")
    output_root = Path(str(manifest["output_root"]))
    products = cast(dict[str, object], manifest["final_products"])
    mask_contract = cast(dict[str, object], manifest["region_mask_contract"])
    region_name = str(mask_contract["region_name"])
    region_count = int(str(mask_contract["selected_grid_point_count"]))
    schema = _regional_hourly_schema(region_name, str(manifest["region_mask_sha256"]))
    all_rows: list[dict[str, object]] = []
    prior: datetime | None = None
    gap_count = 0
    category_totals = np.zeros(len(CATEGORY_CODES), dtype=np.uint64)
    annual_category_totals: defaultdict[int, np.ndarray] = defaultdict(
        lambda: np.zeros(len(CATEGORY_CODES), dtype=np.uint64)
    )
    periods = cast(list[object], manifest["periods"])
    receipts: list[dict[str, object]] = []
    hourly_path = output_root / _safe_relative(products["hourly_counts"], label="hourly counts")
    hourly_path.parent.mkdir(parents=True, exist_ok=True)
    partial = hourly_path.with_name(f".{hourly_path.name}.partial")
    partial.unlink(missing_ok=True)
    writer = pq.ParquetWriter(partial, schema, compression="zstd", version="2.6")
    try:
        for raw in periods:
            period = cast(dict[str, object], raw)
            table = pq.read_table(_regional_output(manifest, period), schema=schema)
            writer.write_table(table)
            rows = table.to_pylist()
            receipt = json.loads(_regional_receipt_path(manifest_path, period).read_text(encoding="utf-8"))
            receipts.append(receipt)
            for row in rows:
                valid = _utc(row["valid_time"])
                if prior is not None and valid != prior + timedelta(hours=1):
                    gap_count += 1
                prior = valid
                for i, code in enumerate(CATEGORY_CODES):
                    value = int(row[f"ptype_{code}_count"])
                    category_totals[i] += value
                    annual_category_totals[valid.year][i] += value
                all_rows.append(row)
        writer.close()
        os.replace(partial, hourly_path)
    except Exception:
        writer.close()
        partial.unlink(missing_ok=True)
        raise
    if not all_rows:
        raise RuntimeError("regional reduction produced no hourly records")
    catalogues = _event_catalogues(all_rows, region_name=region_name, region_count=region_count)
    event_paths = {
        "high_impact": output_root / _safe_relative(products["high_impact_events"], label="high impact events"),
        "freezing_drizzle": output_root / _safe_relative(products["freezing_drizzle_events"], label="freezing drizzle events"),
        "icy_liquid": output_root / _safe_relative(products["icy_liquid_events"], label="icy liquid events"),
    }
    for name, table in catalogues.items():
        _atomic_write_parquet(table, event_paths[name])
    expected_values = len(all_rows) * region_count
    partition_failures = int(int(category_totals.sum()) != expected_values)
    checksum_mismatches = sum(
        int(cast(dict[str, object], receipt["compact_archive"]).get("canonical_receipt_match") is not True)
        for receipt in receipts
    )
    report: dict[str, object] = {
        "schema_version": 1,
        "status": "pass" if gap_count == 0 and partition_failures == 0 and checksum_mismatches == 0 else "critical",
        "region_name": region_name,
        "region_id": manifest["region_id"],
        "region_grid_point_count": region_count,
        "region_mask_sha256": manifest["region_mask_sha256"],
        "region_mask_contract": mask_contract,
        "grain": "one row per valid hour over the selected region",
        "hourly_records": len(all_rows),
        "first_valid_time": _utc(all_rows[0]["valid_time"]).isoformat(),
        "last_valid_time": _utc(all_rows[-1]["valid_time"]).isoformat(),
        "temporal_gap_or_duplicate_count": gap_count,
        "monthly_checksum_mismatch_count": checksum_mismatches,
        "regional_partition_failure_count": partition_failures,
        "canonical_domain_counts_and_message_checksums_matched": True,
        "compact_packing_validated": True,
        "category_totals": {str(code): int(category_totals[i]) for i, code in enumerate(CATEGORY_CODES)},
        "annual_category_totals": {
            str(year): {str(code): int(values[i]) for i, code in enumerate(CATEGORY_CODES)}
            for year, values in sorted(annual_category_totals.items())
        },
        "event_definitions": manifest["event_definitions"],
        "event_counts": {name: table.num_rows for name, table in catalogues.items()},
        "physical_area_limitation": "Cell areas are unavailable; event extent uses selected grid-cell counts and region fractions.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    quality_path = output_root / _safe_relative(products["quality_report"], label="quality report")
    markdown_path = output_root / _safe_relative(products["quality_report_markdown"], label="quality markdown")
    _atomic_write_json(report, quality_path)
    _write_quality_markdown(report, markdown_path)
    if report["status"] != "pass":
        raise RuntimeError(f"regional quality report is critical: {report}")
    product_paths = [hourly_path, *event_paths.values(), quality_path, markdown_path]
    reduction = {
        "schema_version": REGIONAL_REDUCTION_SCHEMA_VERSION,
        "status": "complete",
        "ok": True,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": _sha256_file(manifest_path),
        "products": {
            path.name: {"path": str(path), "size_bytes": path.stat().st_size, "sha256": _sha256_file(path)}
            for path in product_paths
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(reduction, output_root / "REGIONAL_REDUCTION.json")
    return reduction


def write_regional_slurm_scripts(
    *,
    manifest_path: Path,
    array_script_path: Path,
    reduce_script_path: Path,
    concurrency: int = 8,
    partition: str = "pp-long",
    wall_time: str = "00:30:00",
    reduce_partition: str = "pp-short",
    reduce_wall_time: str = "00:30:00",
) -> tuple[Path, Path]:
    manifest = load_regional_manifest(manifest_path)
    periods = cast(list[object], manifest["periods"])
    if concurrency <= 0:
        raise ValueError("concurrency must be positive")
    if not partition.startswith("pp-") or not reduce_partition.startswith("pp-"):
        raise ValueError("regional analysis jobs require pp-* partitions")
    repo_root = Path(__file__).resolve().parents[2]
    logs = manifest_path.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    repo = shlex.quote(str(repo_root))
    manifest_arg = shlex.quote(str(manifest_path.resolve()))
    array_text = f"""#!/usr/bin/env bash
#SBATCH --job-name=ptype-region
#SBATCH --partition={partition}
#SBATCH --time={wall_time}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --array=0-{len(periods) - 1}%{concurrency}
#SBATCH --output={logs.resolve()}/%A_%a.out

set -euo pipefail
[ -f /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh
if [[ -n "${{USER_ENV_ROOT:-}}" ]]; then module use "$USER_ENV_ROOT/modules"; fi
cd {repo}
exec tools/run_balfrin.sh regional-task {manifest_arg} "$SLURM_ARRAY_TASK_ID"
"""
    reduce_text = f"""#!/usr/bin/env bash
#SBATCH --job-name=ptype-region-reduce
#SBATCH --partition={reduce_partition}
#SBATCH --time={reduce_wall_time}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --output={logs.resolve()}/reduce-%j.out

set -euo pipefail
[ -f /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh
if [[ -n "${{USER_ENV_ROOT:-}}" ]]; then module use "$USER_ENV_ROOT/modules"; fi
cd {repo}
exec tools/run_balfrin.sh regional-reduce {manifest_arg}
"""
    for path, content in ((array_script_path, array_text), (reduce_script_path, reduce_text)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    return array_script_path, reduce_script_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build regional PTYPE hourly summaries and event catalogues")
    commands = parser.add_subparsers(dest="command", required=True)
    mask = commands.add_parser("build-mask")
    mask.add_argument("--grid", type=Path, required=True)
    mask.add_argument("--geojson", type=Path, required=True)
    mask.add_argument("--output", type=Path, required=True)
    mask.add_argument("--region-name", required=True)
    mask.add_argument("--boundary-source", required=True)
    mask.add_argument("--feature-property")
    mask.add_argument("--feature-value")
    plan = commands.add_parser("plan")
    plan.add_argument("--analysis-manifest", type=Path, required=True)
    plan.add_argument("--region-mask", type=Path, required=True)
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--output-root", type=Path, required=True)
    plan.add_argument("--region-id", required=True)
    plan.add_argument("--start-period")
    plan.add_argument("--end-period")
    plan.add_argument("--slurm-script", type=Path)
    plan.add_argument("--reduce-slurm-script", type=Path)
    plan.add_argument("--concurrency", type=int, default=8)
    plan.add_argument("--partition", default="pp-long")
    plan.add_argument("--wall-time", default="00:30:00")
    plan.add_argument("--reduce-partition", default="pp-short")
    plan.add_argument("--reduce-wall-time", default="00:30:00")
    task = commands.add_parser("run-task")
    task.add_argument("--manifest", type=Path, required=True)
    task.add_argument("--index", type=int)
    task.add_argument("--lock-timeout-s", type=float, default=0.0)
    reduce = commands.add_parser("reduce")
    reduce.add_argument("--manifest", type=Path, required=True)
    status = commands.add_parser("status")
    status.add_argument("--manifest", type=Path, required=True)
    status.add_argument("--verify-outputs", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "build-mask":
        result = build_region_mask(
            grid_path=args.grid,
            geojson_path=args.geojson,
            output_path=args.output,
            region_name=args.region_name,
            boundary_source=args.boundary_source,
            feature_property=args.feature_property,
            feature_value=args.feature_value,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "plan":
        manifest = build_regional_manifest(
            analysis_manifest_path=args.analysis_manifest,
            region_mask_path=args.region_mask,
            manifest_path=args.manifest,
            output_root=args.output_root,
            region_id=args.region_id,
            start_period=args.start_period,
            end_period=args.end_period,
        )
        array_script = args.slurm_script or args.manifest.with_suffix(".sbatch")
        reduce_script = args.reduce_slurm_script or args.manifest.with_suffix(".reduce.sbatch")
        write_regional_slurm_scripts(
            manifest_path=args.manifest,
            array_script_path=array_script,
            reduce_script_path=reduce_script,
            concurrency=args.concurrency,
            partition=args.partition,
            wall_time=args.wall_time,
            reduce_partition=args.reduce_partition,
            reduce_wall_time=args.reduce_wall_time,
        )
        print(json.dumps({"manifest": manifest, "slurm_script": str(array_script), "reduce_slurm_script": str(reduce_script)}, indent=2))
        return 0
    if args.command == "run-task":
        raw_index = args.index if args.index is not None else os.environ.get("SLURM_ARRAY_TASK_ID")
        if raw_index is None:
            raise RuntimeError("run-task requires --index or SLURM_ARRAY_TASK_ID")
        print(json.dumps(run_regional_task(manifest_path=args.manifest, index=int(raw_index), lock_timeout_s=args.lock_timeout_s), indent=2))
        return 0
    if args.command == "reduce":
        print(json.dumps(reduce_regional_analysis(manifest_path=args.manifest), indent=2))
        return 0
    status = regional_status(manifest_path=args.manifest, verify_outputs=args.verify_outputs)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
