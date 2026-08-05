"""Cycle-aware ICON-REA-L-CH1 inventory and backfill orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Protocol

from .constants import (
    ALGORITHM_FIRDEWSA,
    DIAGNOSTIC_ALGORITHMS,
    INPUT_PARAM_IDS,
    OUTPUT_PARAM_ID,
    OUTPUT_SHORT_NAME,
)
from .gribio import read_grib_archive_metadata
from .operational import (
    MODEL_MEMBERS,
    MODEL_SPECS,
    CycleLock,
    RetryConfig,
    RetryStats,
    _all_member_outputs_valid,
    _atomic_write_json,
    _filter_parts,
    _make_run,
    _retry_operation,
    retry_config_from_options,
    run_operational,
    validate_run_date_time,
)
from .probabilities import member_grib_path

REA_MODEL = "ICON-REA-L-CH1"
MANIFEST_SCHEMA_VERSION = 2
MANIFEST_MODE = "rea_l_ch1_monthly_backfill"
ARCHIVE_PERIOD = "month"
MESSAGES_PER_CYCLE = 24
INVENTORY_CHECKPOINT_SCHEMA_VERSION = 1
INVENTORY_QUERY_MODE = "yearly_fdb_index_depth_2"
LOGGER = logging.getLogger(__name__)
ARCHIVE_METADATA_KEYS = (
    "shortName",
    "paramId",
    "dataDate",
    "dataTime",
    "endStep",
    "validityDate",
    "validityTime",
)


class _Digest(Protocol):
    def update(self, data: bytes) -> None: ...


def _parse_date(value: str) -> datetime:
    validate_run_date_time(value, "0000")
    return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)


def _date_range(start_date: str, end_date: str) -> list[str]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if end < start:
        raise ValueError(f"end_date must be on or after start_date, got {start_date}..{end_date}")
    count = (end - start).days + 1
    return [(start + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(count)]


def _monthly_periods(dates: list[str]) -> list[dict[str, object]]:
    grouped: defaultdict[str, list[str]] = defaultdict(list)
    for date in dates:
        grouped[date[:6]].append(date)
    return [
        {
            "index": index,
            "period": period,
            "dates": period_dates,
            "message_count": len(period_dates) * MESSAGES_PER_CYCLE,
            "archive": f"{REA_MODEL}/{period[:4]}/ptype_{REA_MODEL}_{period}.grib2",
        }
        for index, (period, period_dates) in enumerate(sorted(grouped.items()))
    ]


def _resolved_staging_root(*, staging_root: Path | None, manifest_path: Path, output_root: Path) -> Path:
    resolved_output = output_root.resolve()
    resolved_staging = (manifest_path.parent / "staging" if staging_root is None else staging_root).resolve()
    if (
        resolved_staging == resolved_output
        or resolved_staging.is_relative_to(resolved_output)
        or resolved_output.is_relative_to(resolved_staging)
    ):
        raise ValueError("staging_root and output_root must not overlap")
    return resolved_staging


def _inventory_fields(algorithm: str) -> tuple[tuple[str, str, int], ...]:
    fields: list[tuple[str, str, int]] = [
        ("T", "ml", 24),
        ("P", "ml", 24),
        ("QV", "ml", 24),
        ("HHL", "ml", 0),
        ("TOT_PREC", "sfc", 24),
        ("T_G", "sfc", 24),
    ]
    if algorithm == "icon":
        fields.extend((name, "sfc", 24) for name in ("RAIN_GSP", "SNOW_GSP", "GRAU_GSP"))
    return tuple(fields)


def _year_periods(start_date: str, end_date: str) -> list[tuple[str, str, str]]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    periods: list[tuple[str, str, str]] = []
    for year in range(start.year, end.year + 1):
        period_start = max(start, datetime(year, 1, 1, tzinfo=timezone.utc))
        period_end = min(end, datetime(year, 12, 31, tzinfo=timezone.utc))
        periods.append((str(year), period_start.strftime("%Y%m%d"), period_end.strftime("%Y%m%d")))
    return periods


def _dates_from_compact_fdb_output(output: str) -> set[str]:
    dates: set[str] = set()
    for line in output.splitlines():
        match = re.search(r"(?:^|,)date=([^,]+)", line)
        if match is None:
            if line.strip():
                raise RuntimeError(f"Unexpected compact FDB inventory line {line!r}")
            continue
        values = match.group(1).split("/")
        if len(values) == 3 and values[1] == "to":
            dates.update(_date_range(values[0], values[2]))
            continue
        for value in values:
            if not re.fullmatch(r"\d{8}", value):
                raise RuntimeError(f"Unexpected compact FDB date value {value!r}")
            validate_run_date_time(value, "0000")
            dates.add(value)
    return dates


def _list_index_dates_for_field(
    *,
    name: str,
    levtype: str,
    step: int,
    start_date: str,
    end_date: str,
    retry_config: RetryConfig,
) -> tuple[set[str], RetryStats]:
    retry_stats = RetryStats()
    date_filter = start_date if start_date == end_date else f"{start_date}/to/{end_date}"
    filter_expr = _filter_parts(
        model=REA_MODEL,
        member="000",
        date=date_filter,
        time_value="0000",
        param=INPUT_PARAM_IDS[name],
        levtype=levtype,
        step=str(step),
    )
    result = _retry_operation(
        f"fdb-list compact index for {name} {start_date}..{end_date}",
        lambda: subprocess.run(
            ["fdb-list", "--compact", "--porcelain", "--depth=2", filter_expr],
            check=True,
            capture_output=True,
            text=True,
        ),
        retry_config=retry_config,
        retry_stats=retry_stats,
    )
    dates = _dates_from_compact_fdb_output(result.stdout)
    expected = set(_date_range(start_date, end_date))
    unexpected = sorted(dates - expected)
    if unexpected:
        raise RuntimeError(f"Compact FDB inventory returned dates outside {start_date}..{end_date}: {unexpected[:10]}")
    return dates, retry_stats


def _load_inventory_checkpoint(path: Path, contract: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return {
            "schema_version": INVENTORY_CHECKPOINT_SCHEMA_VERSION,
            "contract": contract,
            "years": {},
        }
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if (
        checkpoint.get("schema_version") != INVENTORY_CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("contract") != contract
        or not isinstance(checkpoint.get("years"), dict)
    ):
        raise RuntimeError(
            f"Inventory checkpoint contract mismatch at {path}; use a new campaign path or remove the stale checkpoint"
        )
    return checkpoint


def _list_available_reanalysis_inventory(
    *,
    algorithm: str,
    start_date: str,
    end_date: str,
    retry_config: RetryConfig,
    retry_stats: RetryStats,
    inventory_workers: int,
    checkpoint_path: Path | None,
) -> tuple[set[str], dict[str, object]]:
    if inventory_workers <= 0:
        raise ValueError(f"inventory_workers must be positive, got {inventory_workers}")
    fields = _inventory_fields(algorithm)
    periods = _year_periods(start_date, end_date)
    contract: dict[str, object] = {
        "query_mode": INVENTORY_QUERY_MODE,
        "algorithm": algorithm,
        "start_date": start_date,
        "end_date": end_date,
        "fields": [
            {"name": name, "levtype": levtype, "sentinel_step": step}
            for name, levtype, step in fields
        ],
    }
    checkpoint = (
        _load_inventory_checkpoint(checkpoint_path, contract)
        if checkpoint_path is not None
        else {"schema_version": INVENTORY_CHECKPOINT_SCHEMA_VERSION, "contract": contract, "years": {}}
    )
    checkpoint_years = checkpoint["years"]
    if not isinstance(checkpoint_years, dict):
        raise RuntimeError("Invalid inventory checkpoint years")
    available_dates: set[str] = set()
    resumed_years = 0
    completed_years = 0
    for year, period_start, period_end in periods:
        cached = checkpoint_years.get(year)
        if isinstance(cached, dict) and isinstance(cached.get("dates"), list):
            period_dates = {str(value) for value in cached["dates"]}
            expected_period_dates = set(_date_range(period_start, period_end))
            if (
                cached.get("start_date") != period_start
                or cached.get("end_date") != period_end
                or any(not re.fullmatch(r"\d{8}", value) for value in period_dates)
                or not period_dates <= expected_period_dates
            ):
                raise RuntimeError(f"Invalid inventory checkpoint entry for year {year} at {checkpoint_path}")
            retry_mapping = cached.get("retry_stats")
            if isinstance(retry_mapping, dict):
                retry_stats.add_mapping(retry_mapping)
            available_dates.update(period_dates)
            resumed_years += 1
            LOGGER.info("resumed REA inventory year=%s dates=%s", year, len(period_dates))
            continue

        LOGGER.info(
            "querying REA inventory year=%s range=%s..%s fields=%s workers=%s",
            year,
            period_start,
            period_end,
            len(fields),
            min(inventory_workers, len(fields)),
        )
        field_dates: list[set[str]] = []
        period_retry_stats = RetryStats()
        with ThreadPoolExecutor(max_workers=min(inventory_workers, len(fields))) as executor:
            futures = {
                executor.submit(
                    _list_index_dates_for_field,
                    name=name,
                    levtype=levtype,
                    step=step,
                    start_date=period_start,
                    end_date=period_end,
                    retry_config=retry_config,
                ): name
                for name, levtype, step in fields
            }
            for future in as_completed(futures):
                dates, field_retry_stats = future.result()
                field_dates.append(dates)
                period_retry_stats.add(field_retry_stats)
        period_dates = set.intersection(*field_dates)
        retry_stats.add(period_retry_stats)
        checkpoint_years[year] = {
            "start_date": period_start,
            "end_date": period_end,
            "dates": sorted(period_dates),
            "retry_stats": period_retry_stats.as_dict(),
        }
        if checkpoint_path is not None:
            _atomic_write_json(checkpoint, checkpoint_path)
        available_dates.update(period_dates)
        completed_years += 1
        LOGGER.info(
            "checkpointed REA inventory year=%s dates=%s retries=%s",
            year,
            len(period_dates),
            period_retry_stats.retries,
        )
    return available_dates, {
        "query_mode": INVENTORY_QUERY_MODE,
        "inventory_workers": inventory_workers,
        "year_periods": len(periods),
        "field_queries": len(periods) * len(fields),
        "executed_field_queries": completed_years * len(fields),
        "resumed_field_queries": resumed_years * len(fields),
        "resumed_years": resumed_years,
        "completed_years": completed_years,
        "checkpoint_path": str(checkpoint_path.resolve()) if checkpoint_path is not None else None,
        "sentinel_contract": contract["fields"],
        "completeness_note": (
            "The index planner proves required-field presence at each field's sentinel step; "
            "daily task retrieval remains authoritative for every required step and level."
        ),
    }


def list_available_reanalysis_dates(
    *,
    algorithm: str = ALGORITHM_FIRDEWSA,
    start_date: str,
    end_date: str,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
    inventory_workers: int = 8,
    checkpoint_path: Path | None = None,
) -> set[str]:
    """Return daily cycles with index-level sentinel presence for every required field."""

    if algorithm not in DIAGNOSTIC_ALGORITHMS:
        raise ValueError(f"Unsupported algorithm {algorithm!r}")
    _date_range(start_date, end_date)
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    dates, _ = _list_available_reanalysis_inventory(
        algorithm=algorithm,
        start_date=start_date,
        end_date=end_date,
        retry_config=config,
        retry_stats=stats,
        inventory_workers=inventory_workers,
        checkpoint_path=checkpoint_path,
    )
    return dates


def build_backfill_manifest(
    *,
    start_date: str,
    end_date: str,
    output_root: Path,
    manifest_path: Path,
    staging_root: Path | None = None,
    algorithm: str = ALGORITHM_FIRDEWSA,
    available_dates: set[str] | None = None,
    allow_missing_dates: bool = False,
    inventory_workers: int = 8,
    fdb_retries: int = 3,
    fdb_retry_initial_s: float = 10.0,
    fdb_retry_max_s: float = 120.0,
) -> dict[str, object]:
    requested_dates = _date_range(start_date, end_date)
    if algorithm not in DIAGNOSTIC_ALGORITHMS:
        raise ValueError(f"Unsupported algorithm {algorithm!r}")
    retry_config = retry_config_from_options(
        retries=fdb_retries,
        initial_delay_s=fdb_retry_initial_s,
        max_delay_s=fdb_retry_max_s,
    )
    retry_stats = RetryStats()
    inventory_checkpoint_path = manifest_path.with_suffix(".inventory.json")
    if available_dates is None:
        inventory, inventory_plan = _list_available_reanalysis_inventory(
            algorithm=algorithm,
            start_date=start_date,
            end_date=end_date,
            retry_config=retry_config,
            retry_stats=retry_stats,
            inventory_workers=inventory_workers,
            checkpoint_path=inventory_checkpoint_path,
        )
    else:
        inventory = available_dates
        inventory_plan = {
            "query_mode": "supplied_dates",
            "inventory_workers": 0,
            "year_periods": len(_year_periods(start_date, end_date)),
            "field_queries": 0,
            "executed_field_queries": 0,
            "resumed_field_queries": 0,
            "resumed_years": 0,
            "completed_years": 0,
            "checkpoint_path": None,
            "sentinel_contract": [],
            "completeness_note": "Dates were supplied by the caller; no live FDB inventory query was performed.",
        }
    missing_dates = [date for date in requested_dates if date not in inventory]
    if missing_dates and not allow_missing_dates:
        preview = ", ".join(missing_dates[:10])
        raise RuntimeError(
            f"REA-L-CH1 inventory is missing {len(missing_dates)} requested cycle(s): {preview}"
            + (" ..." if len(missing_dates) > 10 else "")
        )
    selected_dates = [date for date in requested_dates if date in inventory]
    if not selected_dates:
        raise RuntimeError("No requested REA-L-CH1 cycles are available in FDB")
    start_valid = _parse_date(selected_dates[0]) + timedelta(hours=1)
    end_valid = _parse_date(selected_dates[-1]) + timedelta(days=1)
    periods = _monthly_periods(selected_dates)
    resolved_output_root = output_root.resolve()
    resolved_staging_root = _resolved_staging_root(
        staging_root=staging_root,
        manifest_path=manifest_path,
        output_root=output_root,
    )
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "mode": MANIFEST_MODE,
        "model": REA_MODEL,
        "diagnostic_algorithm": algorithm,
        "output_format": "grib2",
        "members": list(MODEL_MEMBERS[REA_MODEL]),
        "start_step": 1,
        "max_step": MODEL_SPECS[REA_MODEL].max_step,
        "cycle_time": "0000",
        "archive_period": ARCHIVE_PERIOD,
        "archive_ordering": "cycle_date_then_end_step",
        "messages_per_cycle": MESSAGES_PER_CYCLE,
        "cycle_date_range": {"start": start_date, "end": end_date, "inclusive": True},
        "valid_time_coverage": {
            "first_interval_end": start_valid.isoformat(),
            "last_interval_end": end_valid.isoformat(),
            "note": "Cycle D step 24 is valid at D+1 00 UTC; dates in this manifest are cycle dates.",
        },
        "output_root": str(resolved_output_root),
        "staging_root": str(resolved_staging_root),
        "manifest_path": str(manifest_path.resolve()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inventory": {
            "field_contract": [
                "T",
                "P",
                "QV",
                "HHL",
                "TOT_PREC",
                "T_G",
                *(["RAIN_GSP", "SNOW_GSP", "GRAU_GSP"] if algorithm == "icon" else []),
            ],
            "requested_cycles": len(requested_dates),
            "selected_cycles": len(selected_dates),
            "missing_cycles": missing_dates,
            "allow_missing_dates": allow_missing_dates,
            "retry_stats": retry_stats.as_dict(),
            "plan": inventory_plan,
        },
        "periods": periods,
        "file_count_projection": {
            "archive_files": len(periods),
            "archive_contract_files": 1,
            "estimated_archive_root_files": len(periods) + 1,
            "monthly_receipts": len(periods),
            "monthly_locks": len(periods),
            "slurm_logs": len(periods),
            "planner_logs": 1,
            "campaign_control_files": 3,
            "estimated_campaign_root_files": 3 * len(periods) + 4,
            "estimated_persistent_files": 4 * len(periods) + 5,
            "single_message_grib_files_avoided": len(selected_dates) * MESSAGES_PER_CYCLE,
        },
    }
    _atomic_write_json(manifest, manifest_path)
    return manifest


def load_manifest(path: Path) -> dict[str, object]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION or manifest.get("mode") != MANIFEST_MODE:
        raise RuntimeError(f"Unsupported backfill manifest: {path}")
    if (
        manifest.get("model") != REA_MODEL
        or manifest.get("cycle_time") != "0000"
        or manifest.get("archive_period") != ARCHIVE_PERIOD
        or manifest.get("messages_per_cycle") != MESSAGES_PER_CYCLE
    ):
        raise RuntimeError(f"Invalid REA-L-CH1 cycle contract in {path}")
    periods = manifest.get("periods")
    if not isinstance(periods, list) or not periods:
        raise RuntimeError(f"Manifest contains no monthly periods: {path}")
    for index, period in enumerate(periods):
        if not isinstance(period, dict) or period.get("index") != index:
            raise RuntimeError(f"Invalid monthly period at index {index} in {path}")
        value = period.get("period")
        dates = period.get("dates")
        if not isinstance(value, str) or len(value) != 6 or not isinstance(dates, list) or not dates:
            raise RuntimeError(f"Invalid monthly period contract at index {index} in {path}")
        if any(not isinstance(date, str) or date[:6] != value for date in dates):
            raise RuntimeError(f"Monthly period {value} contains an invalid cycle date in {path}")
        if period.get("message_count") != len(dates) * MESSAGES_PER_CYCLE:
            raise RuntimeError(f"Monthly period {value} has an invalid message count in {path}")
    return manifest


def _manifest_period(manifest: dict[str, object], index: int) -> dict[str, object]:
    periods = manifest.get("periods")
    if not isinstance(periods, list) or not (0 <= index < len(periods)):
        raise IndexError(f"Backfill index {index} is outside 0..{len(periods) - 1 if isinstance(periods, list) else -1}")
    period = periods[index]
    if not isinstance(period, dict) or period.get("index") != index:
        raise RuntimeError(f"Invalid monthly period at index {index}")
    return period


def _verified_day_complete(*, output_root: Path, algorithm: str, date: str) -> bool:
    run_dir = output_root / REA_MODEL / date / "0000"
    done_path = run_dir / "DONE.json"
    if not done_path.is_file():
        return False
    try:
        done = json.loads(done_path.read_text(encoding="utf-8"))
        if done.get("monitoring_ok") is not True:
            return False
    except Exception:
        return False
    run = _make_run(REA_MODEL, "000", date, "0000", MODEL_SPECS[REA_MODEL].max_step)
    return _all_member_outputs_valid(
        run=run,
        output_root=output_root,
        output_model=REA_MODEL,
        start_step=1,
        algorithm=algorithm,
        output_format="grib2",
        require_diagnostics=False,
    )


def _archive_path(manifest: dict[str, object], period: dict[str, object]) -> Path:
    relative = Path(str(period["archive"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Invalid archive path in period {period.get('period')}")
    return Path(str(manifest["output_root"])) / relative


def _receipt_path(manifest_path: Path, period: dict[str, object]) -> Path:
    index = period.get("index")
    if not isinstance(index, int):
        raise RuntimeError(f"Invalid index for period {period.get('period')}")
    return manifest_path.parent / "receipts" / f"{index:05d}-{period['period']}.json"


def _expected_archive_messages(period: dict[str, object]) -> list[tuple[str, int]]:
    dates = period.get("dates")
    if not isinstance(dates, list):
        raise RuntimeError(f"Invalid dates for period {period.get('period')}")
    return [(str(date), step) for date in dates for step in range(1, MESSAGES_PER_CYCLE + 1)]


def _manifest_dates(manifest: dict[str, object]) -> list[str]:
    periods = manifest.get("periods")
    if not isinstance(periods, list):
        raise RuntimeError("Backfill manifest does not contain monthly periods")
    return [
        str(date)
        for period in periods
        if isinstance(period, dict)
        for date in period.get("dates", [])
    ]


def _inspect_monthly_archive(path: Path, period: dict[str, object]) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size < 4:
        raise RuntimeError(f"Monthly archive is missing or empty: {path}")
    with path.open("rb") as handle:
        handle.seek(-4, os.SEEK_END)
        if handle.read(4) != b"7777":
            raise RuntimeError(f"Monthly archive has an invalid GRIB terminator: {path}")
    metadata = read_grib_archive_metadata(path, ARCHIVE_METADATA_KEYS)
    expected = _expected_archive_messages(period)
    if len(metadata) != len(expected):
        raise RuntimeError(f"Monthly archive {path} contains {len(metadata)} messages; expected {len(expected)}")
    for position, (message, (date, step)) in enumerate(zip(metadata, expected, strict=True), start=1):
        valid = _parse_date(date) + timedelta(hours=step)
        expected_metadata: dict[str, object] = {
            "shortName": OUTPUT_SHORT_NAME,
            "paramId": OUTPUT_PARAM_ID,
            "dataDate": int(date),
            "dataTime": 0,
            "endStep": step,
            "validityDate": int(valid.strftime("%Y%m%d")),
            "validityTime": int(valid.strftime("%H%M")),
        }
        mismatches = {
            key: {"actual": message.get(key), "expected": value}
            for key, value in expected_metadata.items()
            if str(message.get(key)) != str(value)
        }
        if mismatches:
            raise RuntimeError(f"Monthly archive {path} message {position} metadata mismatch: {mismatches}")
    return {
        "archive_path": str(path),
        "size_bytes": path.stat().st_size,
        "message_count": len(metadata),
        "first_message": metadata[0],
        "last_message": metadata[-1],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _staging_prefix(manifest_path: Path, index: int, period: str) -> str:
    manifest_key = hashlib.sha256(str(manifest_path.resolve()).encode()).hexdigest()[:12]
    return f"ptype-{manifest_key}-{index:05d}-{period}-"


def _remove_stale_staging(staging_root: Path, prefix: str) -> None:
    for stale in staging_root.glob(f"{prefix}*"):
        if stale.is_symlink():
            stale.unlink()
        elif stale.is_dir():
            shutil.rmtree(stale)


def _append_file(source: Path, destination: BinaryIO, digest: _Digest) -> None:
    with source.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            destination.write(chunk)
            digest.update(chunk)


def _archive_receipt_complete(
    *,
    manifest_path: Path,
    manifest: dict[str, object],
    period: dict[str, object],
    verify_outputs: bool,
) -> bool:
    receipt_path = _receipt_path(manifest_path, period)
    archive_path = _archive_path(manifest, period)
    if not receipt_path.is_file() or not archive_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("ok") is not True or str(receipt.get("archive_path")) != str(archive_path):
            return False
        receipt_size = receipt.get("size_bytes")
        receipt_messages = receipt.get("message_count")
        expected_messages = period.get("message_count")
        if not isinstance(receipt_size, int) or receipt_size != archive_path.stat().st_size:
            return False
        if not isinstance(receipt_messages, int) or not isinstance(expected_messages, int):
            return False
        if receipt_messages != expected_messages:
            return False
        if verify_outputs:
            _inspect_monthly_archive(archive_path, period)
            receipt_sha256 = receipt.get("sha256")
            if not isinstance(receipt_sha256, str) or receipt_sha256 != _sha256_file(archive_path):
                return False
    except Exception:
        return False
    return True


def _ensure_archive_contract(manifest: dict[str, object]) -> None:
    dates = _manifest_dates(manifest)
    contract = {
        "schema_version": 1,
        "model": REA_MODEL,
        "diagnostic_algorithm": manifest["diagnostic_algorithm"],
        "output_format": "grib2_multi_message",
        "archive_period": ARCHIVE_PERIOD,
        "archive_ordering": "cycle_date_then_end_step",
        "cycle_time": "0000",
        "start_step": 1,
        "max_step": MESSAGES_PER_CYCLE,
        "shortName": OUTPUT_SHORT_NAME,
        "paramId": OUTPUT_PARAM_ID,
        "cycle_date_range": manifest["cycle_date_range"],
        "selected_cycles": len(dates),
        "cycle_dates_sha256": hashlib.sha256("\n".join(dates).encode()).hexdigest(),
    }
    path = Path(str(manifest["output_root"])) / REA_MODEL / "ARCHIVE_CONTRACT.json"
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Cannot read archive contract {path}: {exc}") from exc
        if existing != contract:
            raise RuntimeError(f"Archive contract mismatch at {path}; use a separate output root")
        return
    _atomic_write_json(contract, path)


def run_manifest_task(
    *,
    manifest_path: Path,
    index: int,
    workers: int = 1,
    chunk_size: int = 2,
    fdb_retries: int = 3,
    fdb_retry_initial_s: float = 10.0,
    fdb_retry_max_s: float = 120.0,
    lock_timeout_s: float = 0.0,
) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    period = _manifest_period(manifest, index)
    period_name = str(period["period"])
    dates = period.get("dates")
    if not isinstance(dates, list):
        raise RuntimeError(f"Invalid cycle dates for monthly period {period_name}")
    staging_root = Path(str(manifest["staging_root"]))
    algorithm = str(manifest["diagnostic_algorithm"])
    receipt_path = _receipt_path(manifest_path, period)
    archive_path = _archive_path(manifest, period)
    partial_path = archive_path.with_name(f".{archive_path.name}.partial")
    lock = CycleLock(manifest_path.parent / "locks" / f"{index:05d}-{period_name}.lock", timeout_s=lock_timeout_s)
    lock.acquire()
    attempt = 1
    try:
        _ensure_archive_contract(manifest)
        previous_receipt: dict[str, object] = {}
        if receipt_path.is_file():
            try:
                loaded_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if isinstance(loaded_receipt, dict):
                    previous_receipt = loaded_receipt
            except Exception:
                previous_receipt = {}
        if archive_path.is_file():
            try:
                archive_info = _inspect_monthly_archive(archive_path, period)
            except Exception:
                pass
            else:
                partial_path.unlink(missing_ok=True)
                receipt = {
                    "index": index,
                    "period": period_name,
                    "dates": dates,
                    "status": "verified_existing",
                    "ok": True,
                    "diagnostic_algorithm": algorithm,
                    "algorithm_fidelity": previous_receipt.get("algorithm_fidelity"),
                    "provenance": previous_receipt.get("provenance"),
                    "daily_results": previous_receipt.get("daily_results"),
                    **archive_info,
                    "sha256": _sha256_file(archive_path),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                _atomic_write_json(receipt, receipt_path)
                return receipt

        previous_attempt = previous_receipt.get("attempt", 0)
        attempt = previous_attempt + 1 if isinstance(previous_attempt, int) else 1

        staging_root.mkdir(parents=True, exist_ok=True)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path.unlink(missing_ok=True)
        staging_prefix = _staging_prefix(manifest_path, index, period_name)
        _remove_stale_staging(staging_root, staging_prefix)
        digest = hashlib.sha256()
        day_results: list[dict[str, object]] = []
        algorithm_fidelity: object = None
        provenance: object = None
        with tempfile.TemporaryDirectory(prefix=staging_prefix, dir=staging_root) as temporary:
            task_staging_root = Path(temporary)
            with partial_path.open("wb") as archive_handle:
                for raw_date in dates:
                    date = str(raw_date)
                    summary = run_operational(
                        model=REA_MODEL,
                        output_root=task_staging_root,
                        algorithm=algorithm,
                        members=("000",),
                        date=date,
                        time_value="0000",
                        start_step=1,
                        max_step=MODEL_SPECS[REA_MODEL].max_step,
                        workers=workers,
                        chunk_size=chunk_size,
                        check_output_files=True,
                        write_probability_products=False,
                        output_format="grib2",
                        run_id=f"rea-l-ch1-{date}",
                        event_id=f"{manifest_path.name}:{index}:{period_name}",
                        attempt=attempt,
                        fdb_retries=fdb_retries,
                        fdb_retry_initial_s=fdb_retry_initial_s,
                        fdb_retry_max_s=fdb_retry_max_s,
                        resume=True,
                        lock_timeout_s=0.0,
                    )
                    monitoring = summary.get("monitoring", {})
                    if not isinstance(monitoring, dict) or monitoring.get("ok") is not True:
                        raise RuntimeError(f"REA-L-CH1 daily cycle {date} failed monitoring")
                    if not _verified_day_complete(output_root=task_staging_root, algorithm=algorithm, date=date):
                        raise RuntimeError(f"REA-L-CH1 daily cycle {date} failed output verification")
                    if algorithm_fidelity is None:
                        algorithm_fidelity = summary.get("algorithm_fidelity")
                    if provenance is None:
                        provenance = summary.get("provenance")
                    for step in range(1, MESSAGES_PER_CYCLE + 1):
                        source = member_grib_path(task_staging_root, REA_MODEL, date, "0000", "000", step)
                        _append_file(source, archive_handle, digest)
                    day_results.append(
                        {
                            "date": date,
                            "wall_s": summary.get("wall_s"),
                            "data_quality": summary.get("data_quality"),
                            "retry_stats": summary.get("retry_stats"),
                        }
                    )
                    shutil.rmtree(task_staging_root / REA_MODEL / date)
                archive_handle.flush()
                os.fsync(archive_handle.fileno())

            archive_info = _inspect_monthly_archive(partial_path, period)
            os.replace(partial_path, archive_path)
            archive_info["archive_path"] = str(archive_path)
        receipt = {
            "index": index,
            "period": period_name,
            "dates": dates,
            "status": "complete",
            "ok": True,
            "attempt": attempt,
            "diagnostic_algorithm": algorithm,
            "algorithm_fidelity": algorithm_fidelity,
            "provenance": provenance,
            **archive_info,
            "sha256": digest.hexdigest(),
            "daily_results": day_results,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(receipt, receipt_path)
        return receipt
    except Exception as exc:
        partial_path.unlink(missing_ok=True)
        receipt = {
            "index": index,
            "period": period_name,
            "dates": dates,
            "status": "critical",
            "ok": False,
            "attempt": attempt,
            "diagnostic_algorithm": algorithm,
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(receipt, receipt_path)
        raise
    finally:
        lock.release()


def campaign_status(*, manifest_path: Path, verify_outputs: bool = False) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    periods = manifest["periods"]
    if not isinstance(periods, list):
        raise RuntimeError(f"Invalid monthly periods in {manifest_path}")
    complete: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    complete_cycles = 0
    failed_dates: list[str] = []
    pending_dates: list[str] = []
    for period in periods:
        if not isinstance(period, dict):
            raise RuntimeError(f"Invalid monthly period entry in {manifest_path}")
        period_name = str(period["period"])
        dates = [str(value) for value in period.get("dates", [])]
        receipt_path = _receipt_path(manifest_path, period)
        receipt_status: object = None
        if receipt_path.is_file():
            try:
                receipt_status = json.loads(receipt_path.read_text(encoding="utf-8")).get("status")
            except Exception:
                receipt_status = None
        if receipt_status == "critical":
            failed.append(period_name)
            failed_dates.extend(dates)
        elif _archive_receipt_complete(
            manifest_path=manifest_path,
            manifest=manifest,
            period=period,
            verify_outputs=verify_outputs,
        ):
            complete.append(period_name)
            complete_cycles += len(dates)
        else:
            pending.append(period_name)
            pending_dates.extend(dates)
    total_cycles = sum(len(period.get("dates", [])) for period in periods if isinstance(period, dict))
    status: dict[str, object] = {
        "schema_version": 2,
        "manifest_path": str(manifest_path.resolve()),
        "model": REA_MODEL,
        "total_periods": len(periods),
        "complete_periods": len(complete),
        "failed_periods": len(failed),
        "pending_periods": len(pending),
        "total_cycles": total_cycles,
        "complete_cycles": complete_cycles,
        "failed_cycles": len(failed_dates),
        "pending_cycles": len(pending_dates),
        "complete": not failed and not pending,
        "verified_outputs": verify_outputs,
        "failed_period_names": failed,
        "pending_period_names": pending,
        "failed_dates": failed_dates,
        "pending_dates": pending_dates,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(status, manifest_path.parent / "campaign-status.json")
    return status


def write_slurm_array_script(
    *,
    manifest_path: Path,
    script_path: Path,
    concurrency: int = 8,
    partition: str = "pp-long",
    wall_time: str = "06:00:00",
) -> Path:
    manifest = load_manifest(manifest_path)
    periods = manifest["periods"]
    if not isinstance(periods, list):
        raise RuntimeError(f"Invalid monthly periods in {manifest_path}")
    if concurrency <= 0:
        raise ValueError(f"concurrency must be positive, got {concurrency}")
    if not partition.startswith("pp-"):
        raise ValueError(f"CPU backfills require a pp-* partition, got {partition!r}")
    repo_root = Path(__file__).resolve().parents[2]
    quoted_repo_root = shlex.quote(str(repo_root))
    quoted_manifest = shlex.quote(str(manifest_path.resolve()))
    log_dir = manifest_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_pattern = str((log_dir / "%A_%a.out").resolve())
    if any(character.isspace() for character in log_pattern):
        raise ValueError(f"Slurm log path must not contain whitespace: {log_pattern}")
    text = f"""#!/usr/bin/env bash
#SBATCH --job-name=ptype-rea
#SBATCH --partition={partition}
#SBATCH --time={wall_time}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --array=0-{len(periods) - 1}%{concurrency}
#SBATCH --output={log_pattern}

set -euo pipefail
[ -f /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh
if [[ -n "${{USER_ENV_ROOT:-}}" ]]; then
  module use "$USER_ENV_ROOT/modules"
fi
cd {quoted_repo_root}
exec tools/run_balfrin.sh backfill-task {quoted_manifest} "$SLURM_ARRAY_TASK_ID"
"""
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(text, encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan, run, and verify monthly ICON-REA-L-CH1 GRIB2 archives")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--start-date", required=True)
    plan.add_argument("--end-date", required=True)
    plan.add_argument("--output-root", type=Path, required=True)
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--staging-root", type=Path)
    plan.add_argument("--algorithm", choices=DIAGNOSTIC_ALGORITHMS, default=ALGORITHM_FIRDEWSA)
    plan.add_argument("--allow-missing-dates", action="store_true")
    plan.add_argument("--slurm-script", type=Path)
    plan.add_argument("--inventory-workers", type=int, default=8)
    plan.add_argument("--concurrency", type=int, default=8)
    plan.add_argument("--partition", default="pp-long")
    plan.add_argument("--wall-time", default="06:00:00")

    task = subparsers.add_parser("run-task")
    task.add_argument("--manifest", type=Path, required=True)
    task.add_argument("--index", type=int)
    task.add_argument("--workers", type=int, default=1)
    task.add_argument("--chunk-size", type=int, default=2)
    task.add_argument("--fdb-retries", type=int, default=3)
    task.add_argument("--fdb-retry-initial-s", type=float, default=10.0)
    task.add_argument("--fdb-retry-max-s", type=float, default=120.0)
    task.add_argument("--lock-timeout-s", type=float, default=0.0)

    status = subparsers.add_parser("status")
    status.add_argument("--manifest", type=Path, required=True)
    status.add_argument("--verify-outputs", action="store_true")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args()
    if args.command == "plan":
        manifest = build_backfill_manifest(
            start_date=args.start_date,
            end_date=args.end_date,
            output_root=args.output_root,
            manifest_path=args.manifest,
            staging_root=args.staging_root,
            algorithm=args.algorithm,
            allow_missing_dates=args.allow_missing_dates,
            inventory_workers=args.inventory_workers,
        )
        script_path = args.slurm_script or args.manifest.with_suffix(".sbatch")
        write_slurm_array_script(
            manifest_path=args.manifest,
            script_path=script_path,
            concurrency=args.concurrency,
            partition=args.partition,
            wall_time=args.wall_time,
        )
        args.manifest.with_suffix(".inventory.json").unlink(missing_ok=True)
        payload = {"manifest": manifest, "slurm_script": str(script_path)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.command == "run-task":
        raw_index = args.index if args.index is not None else os.environ.get("SLURM_ARRAY_TASK_ID")
        if raw_index is None:
            raise RuntimeError("run-task requires --index or SLURM_ARRAY_TASK_ID")
        receipt = run_manifest_task(
            manifest_path=args.manifest,
            index=int(raw_index),
            workers=args.workers,
            chunk_size=args.chunk_size,
            fdb_retries=args.fdb_retries,
            fdb_retry_initial_s=args.fdb_retry_initial_s,
            fdb_retry_max_s=args.fdb_retry_max_s,
            lock_timeout_s=args.lock_timeout_s,
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if receipt.get("ok") is True else 1
    status = campaign_status(manifest_path=args.manifest, verify_outputs=args.verify_outputs)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
