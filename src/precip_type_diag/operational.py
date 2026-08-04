"""FDB-backed operational runner for the precipitation-type diagnostic."""

from __future__ import annotations

import ast
import fcntl
import getpass
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from random import uniform
from typing import IO, Protocol, TypedDict, TypeVar

# isort: off
import eccodes as _eccodes  # noqa: F401  # Import before earthkit; some FDB uenv builds require it.
import earthkit.data as ekd
# isort: on
import numpy as np

from .constants import (
    ALGORITHM_FIRDEWSA,
    ALGORITHM_ICON,
    DEFAULT_VERTICAL_CUTOFF_M,
    DIAGNOSTIC_ALGORITHMS,
    ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS,
    ICON_REFERENCE_COMMIT,
    ICON_UNAVAILABLE_ONLINE_RATE_COMPONENTS,
    INPUT_PARAM_IDS,
    MEMBER_DIAGNOSTIC_VARIABLES,
    OUTPUT_PARAM_ID,
    OUTPUT_SHORT_NAME,
    PrecipitationTypeCode,
)
from .gribio import (
    check_precip_mask_threshold_mm,
    derive_vertical_level_selection,
    read_categorical_grib,
    read_grib_metadata,
    write_output_grib,
)
from .grid import GridInputs, diagnose_grid_categorical, diagnose_grid_categorical_with_quality, diagnose_grid_probabilities_with_quality
from .monitoring import build_monitoring_status
from .netcdfio import inspect_netcdf, read_netcdf
from .probabilities import (
    disabled_probability_summary,
    generate_probability_products,
    member_grib_path,
    member_netcdf_path,
    write_member_diagnostic_netcdf,
    write_member_ptype_netcdf,
)
from .provenance import collect_runtime_provenance

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
_ACTIVE_RUN_MARKER: ContextVar[tuple[Path, dict[str, object]] | None] = ContextVar(
    "precip_type_diag_active_run_marker",
    default=None,
)
_ACTIVE_CYCLE_LOCK: ContextVar[CycleLock | None] = ContextVar("precip_type_diag_active_cycle_lock", default=None)


@dataclass(frozen=True)
class FdbModelSpec:
    fdb_model: str
    fdb_class: str
    expver: str
    stream: str
    members: tuple[str, ...]
    max_step: int
    uenv_view: str
    fixed_time: str | None = None
    requires_explicit_cycle: bool = False
    fdb_utils_reports_timespan: bool = True
    accumulation_contract: str = "from_start_of_forecast"


MODEL_SPECS = {
    "ICON-CH1-EPS": FdbModelSpec(
        fdb_model="icon-ch1-eps",
        fdb_class="od",
        expver="0001",
        stream="enfo",
        members=tuple(f"{member:03d}" for member in range(11)),
        max_step=45,
        uenv_view="realtime",
    ),
    "ICON-CH2-EPS": FdbModelSpec(
        fdb_model="icon-ch2-eps",
        fdb_class="od",
        expver="0001",
        stream="enfo",
        members=tuple(f"{member:03d}" for member in range(21)),
        max_step=120,
        uenv_view="realtime",
    ),
    "ICON-REA-L-CH1": FdbModelSpec(
        fdb_model="icon-rea-l-ch1",
        fdb_class="rd",
        expver="r001",
        stream="reanl",
        members=("000",),
        max_step=24,
        uenv_view="rea-l-ch1",
        fixed_time="0000",
        requires_explicit_cycle=True,
        fdb_utils_reports_timespan=False,
        accumulation_contract="from_0000_daily_cycle_through_step_24",
    ),
}
MODEL_TO_FDB = {model: spec.fdb_model for model, spec in MODEL_SPECS.items()}
MODEL_MAX_STEP = {model: spec.max_step for model, spec in MODEL_SPECS.items()}
MODEL_DEFAULT_MAX_STEP = {**MODEL_MAX_STEP, "ICON-CH1-EPS": 33}
MODEL_MEMBERS = {model: spec.members for model, spec in MODEL_SPECS.items()}
CH1_LONG_HORIZON_CYCLE_TIMES = ("0300",)
DEFAULT_OUTPUT_ROOT = Path(os.environ.get("PRECIP_TYPE_DIAG_OUTPUT_ROOT", "/tmp/precip_type_diag"))
FULL_LEVELS = 80
HALF_LEVELS = 81
ML_PARAMS = (INPUT_PARAM_IDS["T"], INPUT_PARAM_IDS["P"], INPUT_PARAM_IDS["QV"])
PARAM_NAME_BY_ID = {value: key for key, value in INPUT_PARAM_IDS.items()}
TIMING_KEYS = (
    "discovery_s",
    "check_s",
    "static_request_s",
    "static_hhl_request_s",
    "static_prev_precip_request_s",
    "static_decode_s",
    "static_hhl_decode_s",
    "static_prev_precip_decode_s",
    "static_microphysics_request_s",
    "static_microphysics_decode_s",
    "request_s",
    "request_ml_s",
    "request_total_precip_s",
    "request_ground_temperature_s",
    "request_microphysics_s",
    "decode_s",
    "decode_ml_s",
    "decode_total_precip_s",
    "decode_ground_temperature_s",
    "decode_microphysics_s",
    "diagnose_s",
    "write_s",
)


def expected_cycle_max_step(model: str, time_value: str) -> int:
    """Return the reviewed operational horizon for an explicit model cycle."""

    if model not in MODEL_SPECS:
        raise ValueError(f"Unsupported model {model!r}")
    if model == "ICON-CH1-EPS":
        return MODEL_MAX_STEP[model] if time_value in CH1_LONG_HORIZON_CYCLE_TIMES else MODEL_DEFAULT_MAX_STEP[model]
    return MODEL_MAX_STEP[model]


DATA_QUALITY_KEYS = (
    "total_columns",
    "active_columns",
    "invalid_total_precip_columns",
    "invalid_ground_temperature_columns",
    "invalid_profile_columns",
    "invalid_active_ground_temperature_columns",
    "invalid_active_profile_columns",
)
OUTPUT_FORMATS = ("grib2", "netcdf")


class FdbTransientError(RuntimeError):
    """Raised when a transient FDB operation failed after configured retries."""

    def __init__(self, message: str, *, retry_stats: dict[str, int] | None = None) -> None:
        super().__init__(message)
        self.retry_stats = {} if retry_stats is None else retry_stats


class FdbIncompleteError(RuntimeError):
    """Raised when FDB contains an incomplete run for the requested cycle."""


class CycleLockedError(RuntimeError):
    """Raised when another process owns the selected output cycle."""


@dataclass
class CycleLock:
    path: Path
    timeout_s: float = 0.0
    _handle: IO[str] | None = None

    def acquire(self) -> None:
        if self.timeout_s < 0:
            raise ValueError(f"lock timeout must be non-negative, got {self.timeout_s}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise CycleLockedError(f"Cycle is locked by another process: {self.path}") from exc
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        handle.seek(0)
        handle.truncate()
        json.dump(
            {
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
            },
            handle,
            sort_keys=True,
        )
        handle.write("\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class RetryConfig:
    retries: int = 0
    initial_delay_s: float = 10.0
    max_delay_s: float = 120.0

    @property
    def attempts(self) -> int:
        return self.retries + 1


@dataclass
class RetryStats:
    attempts: int = 0
    retries: int = 0
    exhausted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "attempts": int(self.attempts),
            "retries": int(self.retries),
            "exhausted": int(self.exhausted),
        }

    def add(self, other: RetryStats) -> None:
        self.attempts += other.attempts
        self.retries += other.retries
        self.exhausted += other.exhausted

    def add_mapping(self, other: dict[str, int]) -> None:
        self.attempts += int(other.get("attempts", 0))
        self.retries += int(other.get("retries", 0))
        self.exhausted += int(other.get("exhausted", 0))


@dataclass(frozen=True)
class OperationalConfig:
    model: str
    members: tuple[str, ...]
    output_root: Path
    precip_mask_threshold_mm: float
    algorithm: str = ALGORITHM_FIRDEWSA
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M
    start_step: int = 1
    max_step: int = 0
    max_workers: int = 8
    chunk_size: int = 2
    prefetch: bool = True
    output_format: str = "grib2"


@dataclass(frozen=True)
class FdbRun:
    date: str
    time: str
    model: str
    member: str
    type: str
    number: int | None
    max_step: int
    fdb_class: str = "od"
    expver: str = "0001"
    stream: str = "enfo"
    discovery_s: float = 0.0


@dataclass
class Timings:
    discovery_s: float = 0.0
    check_s: float = 0.0
    static_request_s: float = 0.0
    static_hhl_request_s: float = 0.0
    static_prev_precip_request_s: float = 0.0
    static_decode_s: float = 0.0
    static_hhl_decode_s: float = 0.0
    static_prev_precip_decode_s: float = 0.0
    static_microphysics_request_s: float = 0.0
    static_microphysics_decode_s: float = 0.0
    request_s: float = 0.0
    request_ml_s: float = 0.0
    request_total_precip_s: float = 0.0
    request_ground_temperature_s: float = 0.0
    request_microphysics_s: float = 0.0
    decode_s: float = 0.0
    decode_ml_s: float = 0.0
    decode_total_precip_s: float = 0.0
    decode_ground_temperature_s: float = 0.0
    decode_microphysics_s: float = 0.0
    diagnose_s: float = 0.0
    write_s: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {key: round(float(value), 3) for key, value in self.__dict__.items()}


@dataclass(frozen=True)
class FdbChunk:
    steps: list[int]
    ml_by_step: dict[int, dict[str, list[FieldLike]]]
    total_precip_by_step: dict[int, FieldLike]
    ground_temperature_by_step: dict[int, FieldLike]
    request_s: float
    microphysics_accumulations_by_step: dict[str, dict[int, FieldLike]] | None = None
    request_ml_s: float = 0.0
    request_total_precip_s: float = 0.0
    request_ground_temperature_s: float = 0.0
    request_microphysics_s: float = 0.0
    retry_stats: dict[str, int] | None = None


class FieldLike(Protocol):
    def metadata(self, key: str) -> object: ...

    def to_numpy(self, flatten: bool = False) -> np.ndarray: ...


class MemberProcessKwargs(TypedDict):
    model: str
    member: str
    date: str
    time_value: str
    start_step: int
    max_step: int
    output_root: Path
    chunk_size: int
    prefetch: bool
    check_inputs: bool
    precip_mask_threshold_mm: float
    vertical_cutoff_m: float
    algorithm: str
    write_probability_products: bool
    output_format: str
    retry_config: RetryConfig
    resume: bool


def normalize_output_format(value: str) -> str:
    normalized = value.lower()
    if normalized not in OUTPUT_FORMATS:
        supported = ", ".join(OUTPUT_FORMATS)
        raise ValueError(f"Unsupported output_format {value!r}; expected one of: {supported}")
    return normalized


def normalize_algorithm(value: str) -> str:
    normalized = value.lower()
    if normalized not in DIAGNOSTIC_ALGORITHMS:
        supported = ", ".join(DIAGNOSTIC_ALGORITHMS)
        raise ValueError(f"Unsupported algorithm {value!r}; expected one of: {supported}")
    return normalized


def validate_run_date_time(date: str, time_value: str) -> None:
    """Validate fixed FDB run identifiers before using them in requests or paths."""
    if not re.fullmatch(r"\d{8}", date):
        raise ValueError(f"date must use YYYYMMDD, got {date!r}")
    if not re.fullmatch(r"\d{4}", time_value):
        raise ValueError(f"time_value must use HHMM, got {time_value!r}")
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"date must be a valid calendar date, got {date!r}") from exc
    try:
        datetime.strptime(time_value, "%H%M")
    except ValueError as exc:
        raise ValueError(f"time_value must be a valid 24-hour time, got {time_value!r}") from exc


def _duplicate_members(members: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    duplicate_set: set[str] = set()
    duplicates: list[str] = []
    for member in members:
        if member in seen and member not in duplicate_set:
            duplicates.append(member)
            duplicate_set.add(member)
        seen.add(member)
    return duplicates


def config_for_model(
    model: str,
    *,
    members: tuple[str, ...] | None = None,
    output_root: Path | None = None,
    algorithm: str = ALGORITHM_FIRDEWSA,
    precip_mask_threshold_mm: float | None = None,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
    start_step: int = 1,
    max_step: int | None = None,
    workers: int | None = None,
    chunk_size: int = 2,
    prefetch: bool = True,
    output_format: str = "grib2",
) -> OperationalConfig:
    if model not in MODEL_TO_FDB:
        supported = ", ".join(sorted(MODEL_TO_FDB))
        raise ValueError(f"Unsupported model {model!r}; expected one of: {supported}")
    normalized_algorithm = normalize_algorithm(algorithm)
    default_threshold = 0.01 if normalized_algorithm == ALGORITHM_ICON else 0.0
    threshold = check_precip_mask_threshold_mm(default_threshold if precip_mask_threshold_mm is None else precip_mask_threshold_mm)
    effective_max_step = MODEL_DEFAULT_MAX_STEP[model] if max_step is None else max_step
    if effective_max_step < 0:
        raise ValueError(f"max_step must be non-negative, got {effective_max_step}")
    if effective_max_step > MODEL_MAX_STEP[model]:
        raise ValueError(
            f"max_step must be <= {MODEL_MAX_STEP[model]} for {model}, got {effective_max_step}"
        )
    if start_step < 0:
        raise ValueError(f"start_step must be non-negative, got {start_step}")
    if start_step > effective_max_step:
        raise ValueError(f"start_step must be <= max_step, got start_step={start_step} max_step={effective_max_step}")
    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be positive, got {chunk_size}")
    effective_workers = 8 if workers is None else workers
    if effective_workers <= 0:
        raise ValueError(f"workers must be positive, got {effective_workers}")
    if not np.isfinite(float(vertical_cutoff_m)):
        raise ValueError(f"vertical_cutoff_m must be finite, got {vertical_cutoff_m!r}")
    normalized_output_format = normalize_output_format(output_format)

    selected_members = MODEL_MEMBERS[model] if members is None else members
    if not selected_members:
        raise ValueError("At least one member must be selected")
    unknown = [member for member in selected_members if member not in MODEL_MEMBERS[model]]
    if unknown:
        raise ValueError(f"Member(s) not available for {model}: {', '.join(unknown)}")
    duplicates = _duplicate_members(selected_members)
    if duplicates:
        raise ValueError(f"Duplicate member(s): {', '.join(duplicates)}")

    return OperationalConfig(
        model=model,
        members=selected_members,
        output_root=Path(output_root) if output_root is not None else DEFAULT_OUTPUT_ROOT,
        precip_mask_threshold_mm=threshold,
        algorithm=normalized_algorithm,
        vertical_cutoff_m=float(vertical_cutoff_m),
        start_step=int(start_step),
        max_step=effective_max_step,
        max_workers=effective_workers,
        chunk_size=chunk_size,
        prefetch=prefetch,
        output_format=normalized_output_format,
    )


def parse_members(value: str, model: str) -> tuple[str, ...]:
    if value == "all":
        return MODEL_MEMBERS[model]
    members = tuple(item.strip() for item in value.split(",") if item.strip())
    if not members:
        raise ValueError("Expected --members all or a comma-separated list like 000,001")
    invalid = [member for member in members if not re.fullmatch(r"\d{3}", member)]
    if invalid:
        raise ValueError(f"Invalid member identifier(s): {', '.join(invalid)}")
    unknown = [member for member in members if member not in MODEL_MEMBERS[model]]
    if unknown:
        raise ValueError(f"Member(s) not available for {model}: {', '.join(unknown)}")
    duplicates = _duplicate_members(members)
    if duplicates:
        raise ValueError(f"Duplicate member(s): {', '.join(duplicates)}")
    return members


def retry_config_from_options(*, retries: int = 0, initial_delay_s: float = 10.0, max_delay_s: float = 120.0) -> RetryConfig:
    if retries < 0:
        raise ValueError(f"fdb_retries must be non-negative, got {retries}")
    if initial_delay_s <= 0:
        raise ValueError(f"fdb_retry_initial_s must be positive, got {initial_delay_s}")
    if max_delay_s <= 0:
        raise ValueError(f"fdb_retry_max_s must be positive, got {max_delay_s}")
    if max_delay_s < initial_delay_s:
        raise ValueError(
            f"fdb_retry_max_s must be >= fdb_retry_initial_s, got {max_delay_s} < {initial_delay_s}"
        )
    return RetryConfig(
        retries=int(retries),
        initial_delay_s=float(initial_delay_s),
        max_delay_s=float(max_delay_s),
    )


def _retry_operation(
    operation_name: str,
    func: Callable[[], T],
    *,
    retry_config: RetryConfig,
    retry_stats: RetryStats,
) -> T:
    delay_s = retry_config.initial_delay_s
    last_exc: Exception | None = None
    for attempt in range(1, retry_config.attempts + 1):
        retry_stats.attempts += 1
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - infrastructure retries deliberately catch broad FDB failures.
            last_exc = exc
            if attempt >= retry_config.attempts:
                retry_stats.exhausted += 1
                raise FdbTransientError(
                    f"{operation_name} failed after {retry_config.attempts} attempt(s): {type(exc).__name__}: {exc}",
                    retry_stats=retry_stats.as_dict(),
                ) from exc
            retry_stats.retries += 1
            sleep_s = min(delay_s, retry_config.max_delay_s)
            LOGGER.warning(
                "transient operation failed; retrying operation=%s attempt=%s max_attempts=%s sleep_s=%.3f error=%s: %s",
                operation_name,
                attempt,
                retry_config.attempts,
                sleep_s,
                type(exc).__name__,
                exc,
            )
            time.sleep(sleep_s + uniform(0.0, min(sleep_s * 0.1, 1.0)))
            delay_s = min(delay_s * 2.0, retry_config.max_delay_s)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{operation_name} did not run")


def _configure_meteoswiss_definitions() -> None:
    if os.environ.get("PRECIP_TYPE_DIAG_COSMO_DEFS"):
        return
    for candidate in os.environ.get("GRIB_DEFINITION_PATH", "").split(":"):
        path = Path(candidate)
        if path.exists() and ("definitions.edzw" in path.name or "eccodes-cosmo" in str(path)):
            os.environ["PRECIP_TYPE_DIAG_COSMO_DEFS"] = str(path)
            return


def _member_keys(member: str) -> tuple[str, int | None]:
    if member == "000":
        return "cf", None
    if not re.fullmatch(r"\d{3}", member):
        raise ValueError(f"member must use ICON member format like 000 or 001, got {member!r}")
    return "pf", int(member)


def _base_request(run: FdbRun) -> dict[str, object]:
    request: dict[str, object] = {
        "class": run.fdb_class,
        "expver": run.expver,
        "stream": run.stream,
        "model": run.model,
        "type": run.type,
        "date": run.date,
        "time": run.time,
    }
    if run.number is not None:
        request["number"] = run.number
    return request


def _filter_parts(
    *,
    model: str,
    member: str,
    date: str | None = None,
    time_value: str | None = None,
    param: int | None = None,
    levtype: str | None = None,
    step: str | None = None,
) -> str:
    spec = MODEL_SPECS[model]
    type_value, number = _member_keys(member)
    parts = [
        f"class={spec.fdb_class}",
        f"stream={spec.stream}",
        f"expver={spec.expver}",
        f"model={spec.fdb_model}",
        f"type={type_value}",
    ]
    if number is not None:
        parts.append(f"number={number}")
    if date is not None:
        parts.append(f"date={date}")
    if time_value is not None:
        parts.append(f"time={time_value}")
    if param is not None:
        parts.append(f"param={param}")
    if levtype is not None:
        parts.append(f"levtype={levtype}")
    if step is not None:
        parts.append(f"step={step}")
    return ",".join(parts)


def _fdb_utils_list(
    filter_expr: str,
    *,
    show_keys: tuple[str, ...] | None = None,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> dict[str, list[object]]:
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats

    result = _retry_operation(
        "fdb-utils list",
        lambda: subprocess.run(
            ["fdb-utils", "list", *([",".join(show_keys)] if show_keys else []), "--filter", filter_expr],
            check=True,
            capture_output=True,
            text=True,
        ),
        retry_config=config,
        retry_stats=stats,
    )
    values: dict[str, list[object]] = {}
    for line in result.stdout.splitlines():
        if ":" not in line or line.startswith("Keys/Values"):
            continue
        key, raw_value = line.split(":", 1)
        raw_value = raw_value.strip()
        if raw_value.startswith("["):
            values[key.strip()] = ast.literal_eval(raw_value)
    return values


def _parse_step(value: object) -> int:
    text = str(value)
    if text.endswith("m"):
        minutes = int(text[:-1])
        if minutes % 60 != 0:
            raise ValueError(f"Only hourly steps are supported, got {value!r}")
        return minutes // 60
    if text.endswith("h"):
        return int(text[:-1])
    return int(float(text))


def _parse_steps(values: Iterable[object]) -> set[int]:
    return {_parse_step(value) for value in values}


def _parse_levels(values: Iterable[object]) -> set[int]:
    return {int(float(str(value))) for value in values}


def _has_complete_param(
    *,
    model: str,
    member: str,
    date: str,
    time_value: str,
    param: int,
    levtype: str,
    timespan: str,
    expected_steps: set[int],
    expected_levels: set[int] | None = None,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> bool:
    ordered_steps = sorted(expected_steps)
    step_filter = _step_expr(list(range(ordered_steps[0], ordered_steps[-1] + 1)))
    values = _fdb_utils_list(
        _filter_parts(
            model=model,
            member=member,
            date=date,
            time_value=time_value,
            param=param,
            levtype=levtype,
            step=step_filter,
        ),
        retry_config=retry_config,
        retry_stats=retry_stats,
    )
    if MODEL_SPECS[model].fdb_utils_reports_timespan and timespan not in {
        str(value) for value in values.get("timespan", [])
    }:
        return False
    if not expected_steps.issubset(_parse_steps(values.get("step", []))):
        return False
    if expected_levels is not None and not expected_levels.issubset(_parse_levels(values.get("levelist", []))):
        return False
    return True


def _check_complete_run(
    model: str,
    member: str,
    date: str,
    time_value: str,
    max_step: int,
    start_step: int = 1,
    algorithm: str = ALGORITHM_FIRDEWSA,
    *,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> None:
    algorithm = normalize_algorithm(algorithm)
    diagnostic_steps = set(range(start_step, max_step + 1))
    total_precip_steps = set(range(max(0, start_step - 1), max_step + 1))
    expected_full_levels = set(range(1, FULL_LEVELS + 1))
    expected_half_levels = set(range(1, HALF_LEVELS + 1))
    checks = [
        (
            "HHL",
            _has_complete_param(
                model=model,
                member=member,
                date=date,
                time_value=time_value,
                param=INPUT_PARAM_IDS["HHL"],
                levtype="ml",
                timespan="none",
                expected_steps={0},
                expected_levels=expected_half_levels,
                retry_config=retry_config,
                retry_stats=retry_stats,
            ),
        ),
        (
            "TOT_PREC",
            _has_complete_param(
                model=model,
                member=member,
                date=date,
                time_value=time_value,
                param=INPUT_PARAM_IDS["TOT_PREC"],
                levtype="sfc",
                timespan="fs",
                expected_steps=total_precip_steps,
                retry_config=retry_config,
                retry_stats=retry_stats,
            ),
        ),
        (
            "T_G",
            _has_complete_param(
                model=model,
                member=member,
                date=date,
                time_value=time_value,
                param=INPUT_PARAM_IDS["T_G"],
                levtype="sfc",
                timespan="none",
                expected_steps=diagnostic_steps,
                retry_config=retry_config,
                retry_stats=retry_stats,
            ),
        ),
    ]
    for param in ML_PARAMS:
        checks.append(
            (
                str(param),
                _has_complete_param(
                    model=model,
                    member=member,
                    date=date,
                    time_value=time_value,
                    param=param,
                    levtype="ml",
                    timespan="none",
                    expected_steps=diagnostic_steps,
                    expected_levels=expected_full_levels,
                    retry_config=retry_config,
                    retry_stats=retry_stats,
                ),
            )
        )
    if algorithm == ALGORITHM_ICON:
        for name in ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS:
            checks.append(
                (
                    name,
                    _has_complete_param(
                        model=model,
                        member=member,
                        date=date,
                        time_value=time_value,
                        param=INPUT_PARAM_IDS[name],
                        levtype="sfc",
                        timespan="fs",
                        expected_steps=total_precip_steps,
                        retry_config=retry_config,
                        retry_stats=retry_stats,
                    ),
                )
            )
    missing = [name for name, ok in checks if not ok]
    if missing:
        raise FdbIncompleteError(
            f"Incomplete FDB run for {model} member {member} at {date} {time_value}; "
            f"missing or incomplete: {', '.join(missing)}"
        )


def discover_complete_run(
    *,
    model: str,
    member: str,
    start_step: int,
    max_step: int,
    lookback_days: int,
    algorithm: str = ALGORITHM_FIRDEWSA,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> FdbRun:
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    start = time.perf_counter()
    today = datetime.now(timezone.utc).date()
    candidates: list[tuple[str, str]] = []
    for offset in range(lookback_days + 1):
        date = (today - timedelta(days=offset)).strftime("%Y%m%d")
        values = _fdb_utils_list(
            _filter_parts(
                model=model,
                member=member,
                date=date,
                param=INPUT_PARAM_IDS["TOT_PREC"],
                levtype="sfc",
            ),
            retry_config=config,
            retry_stats=stats,
        )
        for time_value in values.get("time", []):
            candidates.append((date, str(time_value)))

    for date, time_value in sorted(candidates, reverse=True):
        try:
            _check_complete_run(
                model,
                member,
                date,
                time_value,
                max_step,
                start_step=start_step,
                algorithm=algorithm,
                retry_config=config,
                retry_stats=stats,
            )
        except FdbIncompleteError:
            continue
        return _make_run(
            model,
            member,
            date,
            time_value,
            max_step,
            discovery_s=time.perf_counter() - start,
        )

    raise RuntimeError(
        f"No complete realtime FDB run found for {model} member {member} "
        f"with steps 0..{max_step} in the last {lookback_days} day(s)"
    )


def _make_run(
    model: str,
    member: str,
    date: str,
    time_value: str,
    max_step: int,
    *,
    discovery_s: float = 0.0,
) -> FdbRun:
    spec = MODEL_SPECS[model]
    type_value, number = _member_keys(member)
    return FdbRun(
        date=date,
        time=time_value,
        model=spec.fdb_model,
        member=member,
        type=type_value,
        number=number,
        max_step=max_step,
        fdb_class=spec.fdb_class,
        expver=spec.expver,
        stream=spec.stream,
        discovery_s=discovery_s,
    )


def _request_fieldlist(
    request: dict[str, object],
    *,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> tuple[Iterable[FieldLike], float]:
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    start = time.perf_counter()
    fieldlist = _retry_operation(
        "fdb field retrieval",
        lambda: ekd.from_source("fdb", request, stream=True).to_fieldlist(),
        retry_config=config,
        retry_stats=stats,
    )
    return fieldlist, time.perf_counter() - start


def _field_to_numpy(
    field: FieldLike,
    *,
    retry_config: RetryConfig,
    retry_stats: RetryStats,
    flatten: bool = False,
) -> np.ndarray:
    return _retry_operation(
        "fdb field decode",
        lambda: field.to_numpy(flatten=flatten),
        retry_config=retry_config,
        retry_stats=retry_stats,
    )


def _level(field: FieldLike) -> int:
    return int(float(str(field.metadata("level"))))


def _step(field: FieldLike) -> int:
    try:
        return _parse_step(field.metadata("endStep"))
    except Exception:
        return _parse_step(field.metadata("step"))


def _materialize_fields(fieldlist: Iterable[FieldLike]) -> list[FieldLike]:
    """Consume an Earthkit stream without requesting its unsupported length."""

    return [field for field in fieldlist]


def _fields_by_step(fieldlist: Iterable[FieldLike]) -> dict[int, FieldLike]:
    result: dict[int, FieldLike] = {}
    for field in fieldlist:
        result[_step(field)] = field
    return result


def _icon_microphysics_fields_by_step(fieldlist: Iterable[FieldLike]) -> dict[str, dict[int, FieldLike]]:
    expected_names = set(ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS)
    result: dict[str, dict[int, FieldLike]] = {name: {} for name in ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS}
    for field in fieldlist:
        param_id = int(str(field.metadata("paramId")))
        name = PARAM_NAME_BY_ID.get(param_id)
        if name not in expected_names:
            raise RuntimeError(f"Unexpected ICON microphysics accumulation paramId {param_id}")
        result[name][_step(field)] = field
    return result


def _ml_fields_by_step(fieldlist: Iterable[FieldLike]) -> dict[int, dict[str, list[FieldLike]]]:
    result: dict[int, dict[str, list[FieldLike]]] = {}
    for field in fieldlist:
        param_id = int(str(field.metadata("paramId")))
        name = PARAM_NAME_BY_ID[param_id]
        result.setdefault(_step(field), {}).setdefault(name, []).append(field)
    return result


def _stack_level_fields(
    fields: list[FieldLike],
    expected_count: int,
    *,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> np.ndarray:
    ordered = sorted(fields, key=_level)
    if len(ordered) != expected_count:
        raise RuntimeError(f"Expected {expected_count} fields, got {len(ordered)}")
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    return np.stack(
        [_field_to_numpy(field, retry_config=config, retry_stats=stats, flatten=False) for field in ordered],
        axis=0,
    )


def _step_expr(steps: list[int]) -> str:
    if len(steps) == 1:
        return str(steps[0])
    if steps != list(range(steps[0], steps[-1] + 1)):
        raise ValueError(f"Step chunks must be contiguous, got {steps}")
    return f"{steps[0]}/to/{steps[-1]}/by/1"


def _step_token(step: int) -> str:
    days, hours = divmod(step, 24)
    return f"{days:02d}{hours:02d}0000"


def _warm_diagnostic(algorithm: str = ALGORITHM_FIRDEWSA) -> None:
    diagnose_grid_categorical(
        GridInputs(
            temperature_k=np.full((2, 1), 270.0),
            pressure_pa=np.full((2, 1), 80000.0),
            specific_humidity=np.full((2, 1), 0.002),
            half_level_height_m=np.array([[2000.0], [1000.0], [0.0]]),
            total_precip_mm=np.array([1.0]),
            ground_temperature_c=np.array([-1.0]),
        ),
        algorithm=algorithm,
        precip_mask_threshold_mm=0.01 if algorithm == ALGORITHM_ICON else 0.0,
    )


def _fetch_hhl(run: FdbRun, timings: Timings, *, retry_config: RetryConfig, retry_stats: RetryStats) -> np.ndarray:
    request = {
        **_base_request(run),
        "param": INPUT_PARAM_IDS["HHL"],
        "levtype": "ml",
        "levelist": f"1/to/{HALF_LEVELS}",
        "step": "0",
        "timespan": "none",
    }
    fieldlist, request_s = _request_fieldlist(request, retry_config=retry_config, retry_stats=retry_stats)
    timings.static_request_s += request_s
    timings.static_hhl_request_s += request_s
    start = time.perf_counter()
    hhl = _stack_level_fields(_materialize_fields(fieldlist), HALF_LEVELS, retry_config=retry_config, retry_stats=retry_stats)
    decode_s = time.perf_counter() - start
    timings.static_decode_s += decode_s
    timings.static_hhl_decode_s += decode_s
    return hhl


def _fetch_total_precip_step(
    run: FdbRun,
    *,
    step: int,
    timings: Timings,
    retry_config: RetryConfig,
    retry_stats: RetryStats,
) -> np.ndarray:
    request = {
        **_base_request(run),
        "param": INPUT_PARAM_IDS["TOT_PREC"],
        "levtype": "sfc",
        "step": str(step),
        "timespan": "fs",
    }
    fieldlist, request_s = _request_fieldlist(request, retry_config=retry_config, retry_stats=retry_stats)
    timings.static_request_s += request_s
    timings.static_prev_precip_request_s += request_s
    fields = _materialize_fields(fieldlist)
    if len(fields) != 1:
        raise RuntimeError(f"Expected one TOT_PREC field for initialization step {step}, got {len(fields)}")
    start = time.perf_counter()
    values = _field_to_numpy(fields[0], retry_config=retry_config, retry_stats=retry_stats, flatten=False)
    decode_s = time.perf_counter() - start
    timings.static_decode_s += decode_s
    timings.static_prev_precip_decode_s += decode_s
    return values


def _fetch_icon_microphysics_step(
    run: FdbRun,
    *,
    step: int,
    timings: Timings,
    retry_config: RetryConfig,
    retry_stats: RetryStats,
) -> dict[str, np.ndarray]:
    request = {
        **_base_request(run),
        "param": "/".join(str(INPUT_PARAM_IDS[name]) for name in ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS),
        "levtype": "sfc",
        "step": str(step),
        "timespan": "fs",
    }
    fieldlist, request_s = _request_fieldlist(request, retry_config=retry_config, retry_stats=retry_stats)
    timings.static_request_s += request_s
    timings.static_microphysics_request_s += request_s
    fields = _icon_microphysics_fields_by_step(fieldlist)
    missing = [name for name, by_step in fields.items() if step not in by_step]
    if missing:
        raise RuntimeError(f"Missing ICON microphysics initialization field(s) at step {step}: {', '.join(missing)}")
    start = time.perf_counter()
    values = {
        name: _field_to_numpy(by_step[step], retry_config=retry_config, retry_stats=retry_stats, flatten=False)
        for name, by_step in fields.items()
    }
    decode_s = time.perf_counter() - start
    timings.static_decode_s += decode_s
    timings.static_microphysics_decode_s += decode_s
    return values


def _record_chunk_request_timings(timings: Timings, chunk: FdbChunk) -> None:
    timings.request_s += chunk.request_s
    timings.request_ml_s += chunk.request_ml_s
    timings.request_total_precip_s += chunk.request_total_precip_s
    timings.request_ground_temperature_s += chunk.request_ground_temperature_s
    timings.request_microphysics_s += chunk.request_microphysics_s


def _fetch_chunk(
    run: FdbRun,
    *,
    steps: list[int],
    full_level_start: int,
    algorithm: str = ALGORITHM_FIRDEWSA,
    timings: Timings | None = None,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> FdbChunk:
    algorithm = normalize_algorithm(algorithm)
    config = RetryConfig() if retry_config is None else retry_config
    owns_retry_stats = retry_stats is None
    stats = RetryStats() if retry_stats is None else retry_stats
    level_start = full_level_start + 1
    step_value = _step_expr(steps)
    base = _base_request(run)
    ml_request = {
        **base,
        "param": "/".join(str(param) for param in ML_PARAMS),
        "levtype": "ml",
        "levelist": f"{level_start}/to/{FULL_LEVELS}",
        "step": step_value,
        "timespan": "none",
    }
    total_precip_request = {
        **base,
        "param": INPUT_PARAM_IDS["TOT_PREC"],
        "levtype": "sfc",
        "step": step_value,
        "timespan": "fs",
    }
    ground_temperature_request = {
        **base,
        "param": INPUT_PARAM_IDS["T_G"],
        "levtype": "sfc",
        "step": step_value,
        "timespan": "none",
    }
    microphysics_request = {
        **base,
        "param": "/".join(str(INPUT_PARAM_IDS[name]) for name in ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS),
        "levtype": "sfc",
        "step": step_value,
        "timespan": "fs",
    }

    ml_fields, request_ml_s = _request_fieldlist(ml_request, retry_config=config, retry_stats=stats)
    total_precip_fields, request_total_precip_s = _request_fieldlist(
        total_precip_request,
        retry_config=config,
        retry_stats=stats,
    )
    ground_temperature_fields, request_ground_temperature_s = _request_fieldlist(
        ground_temperature_request,
        retry_config=config,
        retry_stats=stats,
    )
    microphysics_fields: Iterable[FieldLike] = ()
    request_microphysics_s = 0.0
    if algorithm == ALGORITHM_ICON:
        microphysics_fields, request_microphysics_s = _request_fieldlist(
            microphysics_request,
            retry_config=config,
            retry_stats=stats,
        )
    chunk = FdbChunk(
        steps=steps,
        ml_by_step=_ml_fields_by_step(ml_fields),
        total_precip_by_step=_fields_by_step(total_precip_fields),
        ground_temperature_by_step=_fields_by_step(ground_temperature_fields),
        request_s=request_ml_s + request_total_precip_s + request_ground_temperature_s + request_microphysics_s,
        microphysics_accumulations_by_step=(
            _icon_microphysics_fields_by_step(microphysics_fields) if algorithm == ALGORITHM_ICON else None
        ),
        request_ml_s=request_ml_s,
        request_total_precip_s=request_total_precip_s,
        request_ground_temperature_s=request_ground_temperature_s,
        request_microphysics_s=request_microphysics_s,
        retry_stats=stats.as_dict() if owns_retry_stats else None,
    )
    if timings is not None:
        _record_chunk_request_timings(timings, chunk)
    return chunk


def _chunk_steps(all_steps: list[int], chunk_size: int) -> list[list[int]]:
    return [all_steps[start : start + chunk_size] for start in range(0, len(all_steps), chunk_size)]


def _process_chunk(
    chunk: FdbChunk,
    *,
    timings: Timings,
    retained_full_levels: int,
    half_level_height_m: np.ndarray,
    previous_total_precip: np.ndarray | None,
    previous_icon_accumulations: dict[str, np.ndarray] | None = None,
    output_root: Path,
    run: FdbRun,
    output_model: str,
    precip_mask_threshold_mm: float,
    algorithm: str = ALGORITHM_FIRDEWSA,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
    write_probability_products: bool = False,
    output_format: str = "grib2",
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> tuple[np.ndarray | None, int, int, int, int, dict[str, int]]:
    algorithm = normalize_algorithm(algorithm)
    output_format = normalize_output_format(output_format)
    if write_probability_products and output_format != "netcdf":
        raise ValueError("write_probability_products requires output_format='netcdf'")
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    written = 0
    diagnostic_netcdf_outputs_written = 0
    active_columns = 0
    total_columns = 0
    data_quality = {key: 0 for key in DATA_QUALITY_KEYS}
    icon_accumulation_state = {} if previous_icon_accumulations is None else previous_icon_accumulations
    for step in chunk.steps:
        LOGGER.info("processing member=%s step=%s", run.member, step)
        decode_start = time.perf_counter()
        step_ml = chunk.ml_by_step[step]
        total_precip_decode_start = time.perf_counter()
        total_precip_current = _field_to_numpy(
            chunk.total_precip_by_step[step],
            retry_config=config,
            retry_stats=stats,
            flatten=False,
        )
        timings.decode_total_precip_s += time.perf_counter() - total_precip_decode_start
        ground_temperature_decode_start = time.perf_counter()
        ground_temperature_k = _field_to_numpy(
            chunk.ground_temperature_by_step[step],
            retry_config=config,
            retry_stats=stats,
            flatten=False,
        )
        timings.decode_ground_temperature_s += time.perf_counter() - ground_temperature_decode_start
        total_precip_mm = total_precip_current if previous_total_precip is None else total_precip_current - previous_total_precip
        if algorithm == ALGORITHM_ICON:
            total_precip_mm = np.where(total_precip_mm < 0.0, total_precip_current, total_precip_mm)
            if chunk.microphysics_accumulations_by_step is None:
                raise RuntimeError("ICON mode requires archived microphysics accumulation fields")
            microphysics_decode_start = time.perf_counter()
            current_icon_accumulations: dict[str, np.ndarray] = {}
            icon_rates: dict[str, np.ndarray] = {}
            for name in ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS:
                fields_by_step = chunk.microphysics_accumulations_by_step.get(name, {})
                if step not in fields_by_step:
                    raise RuntimeError(f"ICON mode is missing {name} at step {step}")
                current = _field_to_numpy(
                    fields_by_step[step],
                    retry_config=config,
                    retry_stats=stats,
                    flatten=False,
                )
                previous = icon_accumulation_state.get(name)
                amount = current if previous is None else current - previous
                amount = np.where(amount < 0.0, current, amount)
                current_icon_accumulations[name] = current
                icon_rates[name] = amount / 3600.0
            timings.decode_microphysics_s += time.perf_counter() - microphysics_decode_start
        else:
            current_icon_accumulations = {}
            icon_rates = {}
        ml_decode_start = time.perf_counter()
        temperature_k = _stack_level_fields(
            step_ml["T"],
            retained_full_levels,
            retry_config=config,
            retry_stats=stats,
        )
        pressure_pa = _stack_level_fields(
            step_ml["P"],
            retained_full_levels,
            retry_config=config,
            retry_stats=stats,
        )
        specific_humidity = _stack_level_fields(
            step_ml["QV"],
            retained_full_levels,
            retry_config=config,
            retry_stats=stats,
        )
        timings.decode_ml_s += time.perf_counter() - ml_decode_start
        inputs = GridInputs(
            temperature_k=temperature_k,
            pressure_pa=pressure_pa,
            specific_humidity=specific_humidity,
            half_level_height_m=half_level_height_m,
            total_precip_mm=total_precip_mm,
            ground_temperature_c=ground_temperature_k - 273.15,
            rain_rate_kg_m2_s=icon_rates.get("RAIN_GSP"),
            snow_rate_kg_m2_s=icon_rates.get("SNOW_GSP"),
            graupel_rate_kg_m2_s=icon_rates.get("GRAU_GSP"),
        )
        timings.decode_s += time.perf_counter() - decode_start

        diagnose_start = time.perf_counter()
        if write_probability_products:
            if algorithm == ALGORITHM_ICON:
                diagnostic_result = diagnose_grid_probabilities_with_quality(
                    inputs,
                    precip_mask_threshold_mm=precip_mask_threshold_mm,
                    algorithm=algorithm,
                    vertical_cutoff_m=vertical_cutoff_m,
                )
            else:
                diagnostic_result = diagnose_grid_probabilities_with_quality(
                    inputs,
                    precip_mask_threshold_mm=precip_mask_threshold_mm,
                )
            categorical = diagnostic_result.categorical
            quality_report = diagnostic_result.quality
            probabilities = diagnostic_result.probabilities
        else:
            if algorithm == ALGORITHM_ICON:
                categorical_result = diagnose_grid_categorical_with_quality(
                    inputs,
                    precip_mask_threshold_mm=precip_mask_threshold_mm,
                    algorithm=algorithm,
                    vertical_cutoff_m=vertical_cutoff_m,
                )
            else:
                categorical_result = diagnose_grid_categorical_with_quality(
                    inputs,
                    precip_mask_threshold_mm=precip_mask_threshold_mm,
                )
            categorical = categorical_result.categorical
            quality_report = categorical_result.quality
            probabilities = None
        timings.diagnose_s += time.perf_counter() - diagnose_start
        quality = quality_report.as_dict()
        for key in DATA_QUALITY_KEYS:
            data_quality[key] += int(quality.get(key, 0))
        active_columns += int(quality["active_columns"])
        total_columns += int(quality["total_columns"])

        write_start = time.perf_counter()
        if output_format == "grib2":
            destination = output_root / output_model / run.date / run.time / run.member / f"lfff{_step_token(step)}.ptype.grib2"
            write_output_grib(chunk.total_precip_by_step[step], categorical, destination, expected_shape=tuple(categorical.shape))
        elif output_format == "netcdf":
            destination = output_root / output_model / run.date / run.time / run.member / f"lfff{_step_token(step)}.ptype.nc"
            attrs = {
                "model": output_model,
                "date": run.date,
                "time": run.time,
                "member": run.member,
                "step": step,
                "diagnostic_algorithm": algorithm,
            }
            if algorithm == ALGORITHM_ICON:
                attrs["icon_reference_commit"] = ICON_REFERENCE_COMMIT
            if write_probability_products:
                if probabilities is None:
                    raise RuntimeError("diagnostic probabilities were not computed")
                write_member_diagnostic_netcdf(
                    destination,
                    ptype=categorical,
                    hourly_precip_mm=total_precip_mm,
                    probabilities=probabilities,
                    attrs=attrs,
                )
                diagnostic_netcdf_outputs_written += 1
            else:
                write_member_ptype_netcdf(
                    destination,
                    ptype=categorical,
                    attrs=attrs,
                )
        else:
            raise ValueError(f"Unsupported output_format {output_format!r}")
        timings.write_s += time.perf_counter() - write_start
        written += 1
        previous_total_precip = total_precip_current
        icon_accumulation_state.update(current_icon_accumulations)
    return previous_total_precip, written, diagnostic_netcdf_outputs_written, active_columns, total_columns, data_quality


def _member_output_path(
    output_root: Path,
    model: str,
    date: str,
    time_value: str,
    member: str,
    step: int,
    output_format: str,
) -> Path:
    if output_format == "netcdf":
        return member_netcdf_path(output_root, model, date, time_value, member, step)
    return member_grib_path(output_root, model, date, time_value, member, step)


def _valid_existing_member_output(
    path: Path,
    *,
    model: str,
    date: str,
    time_value: str,
    member: str,
    step: int,
    algorithm: str,
    output_format: str,
    require_diagnostics: bool,
) -> bool:
    if not path.is_file():
        return False
    try:
        if output_format == "grib2":
            read_categorical_grib(path)
            metadata = read_grib_metadata(path, ("shortName", "paramId", "dataDate", "dataTime", "endStep"))
            expected_metadata: dict[str, object] = {
                "shortName": OUTPUT_SHORT_NAME,
                "paramId": OUTPUT_PARAM_ID,
                "dataDate": int(date),
                "dataTime": int(time_value),
                "endStep": step,
            }
            return all(str(metadata.get(key)) == str(value) for key, value in expected_metadata.items())
        attrs, variables = inspect_netcdf(path)
        expected_attrs: dict[str, object] = {
            "model": model,
            "date": date,
            "time": time_value,
            "member": member,
            "step": step,
            "diagnostic_algorithm": algorithm,
        }
        if any(str(attrs.get(key)) != str(value) for key, value in expected_attrs.items()):
            return False
        required_variables = set(MEMBER_DIAGNOSTIC_VARIABLES if require_diagnostics else ("ptype",))
        if not required_variables.issubset(variables):
            return False
        ptype = np.asarray(read_netcdf(path)["ptype"])
        if not np.all(np.isfinite(ptype)) or not np.all(ptype == np.rint(ptype)):
            return False
        allowed = {int(code) for code in PrecipitationTypeCode}
        return not (set(int(value) for value in np.unique(ptype)) - allowed)
    except Exception:
        LOGGER.warning("existing output failed verification and will be regenerated: %s", path, exc_info=True)
        return False


def _all_member_outputs_valid(
    *,
    run: FdbRun,
    output_root: Path,
    output_model: str,
    start_step: int,
    algorithm: str,
    output_format: str,
    require_diagnostics: bool,
) -> bool:
    return all(
        _valid_existing_member_output(
            _member_output_path(output_root, output_model, run.date, run.time, run.member, step, output_format),
            model=output_model,
            date=run.date,
            time_value=run.time,
            member=run.member,
            step=step,
            algorithm=algorithm,
            output_format=output_format,
            require_diagnostics=require_diagnostics,
        )
        for step in range(start_step, run.max_step + 1)
    )


def process_member_run(
    *,
    run: FdbRun,
    output_root: Path,
    output_model: str,
    chunk_size: int,
    prefetch: bool,
    check_inputs: bool,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float,
    start_step: int,
    algorithm: str = ALGORITHM_FIRDEWSA,
    write_probability_products: bool = False,
    output_format: str = "grib2",
    retry_config: RetryConfig | None = None,
    resume: bool = True,
) -> dict[str, object]:
    algorithm = normalize_algorithm(algorithm)
    config = RetryConfig() if retry_config is None else retry_config
    retry_stats = RetryStats()
    normalized_output_format = normalize_output_format(output_format)
    if write_probability_products and normalized_output_format != "netcdf":
        raise ValueError("write_probability_products requires output_format='netcdf'")
    _configure_meteoswiss_definitions()
    expected_step_count = run.max_step - start_step + 1
    if resume and _all_member_outputs_valid(
        run=run,
        output_root=output_root,
        output_model=output_model,
        start_step=start_step,
        algorithm=algorithm,
        output_format=normalized_output_format,
        require_diagnostics=write_probability_products,
    ):
        LOGGER.info(
            "reusing verified member outputs member=%s model=%s date=%s time=%s steps=%s..%s",
            run.member,
            output_model,
            run.date,
            run.time,
            start_step,
            run.max_step,
        )
        return {
            "run": {
                "date": run.date,
                "time": run.time,
                "model": run.model,
                "member": run.member,
                "type": run.type,
                "number": run.number,
                "max_step": run.max_step,
            },
            "chunk_size": chunk_size,
            "prefetch": prefetch,
            "start_step": start_step,
            "algorithm": algorithm,
            "steps": expected_step_count,
            "chunks": 0,
            "static_fdb_request_count": 0,
            "dynamic_fdb_request_count": 0,
            "fdb_request_count": 0,
            "fdb_utils_check_count": 0,
            "written": expected_step_count,
            "newly_written": 0,
            "reused": expected_step_count,
            "resumed": True,
            "diagnostic_netcdf_outputs_written": expected_step_count if write_probability_products else 0,
            "retained_full_levels": None,
            "active_columns": 0,
            "total_columns": 0,
            "data_quality": {key: 0 for key in DATA_QUALITY_KEYS},
            "retry_stats": RetryStats().as_dict(),
            "timings_s": Timings(discovery_s=run.discovery_s).as_dict(),
            "wall_s": 0.0,
        }
    if algorithm == ALGORITHM_ICON:
        _warm_diagnostic(algorithm)
    else:
        _warm_diagnostic()
    LOGGER.info(
        "starting member=%s model=%s date=%s time=%s max_step=%s chunk_size=%s prefetch=%s",
        run.member,
        output_model,
        run.date,
        run.time,
        run.max_step,
        chunk_size,
        prefetch,
    )
    timings = Timings(discovery_s=run.discovery_s)
    wall_start = time.perf_counter()
    if check_inputs:
        check_start = time.perf_counter()
        _check_complete_run(
            output_model,
            run.member,
            run.date,
            run.time,
            run.max_step,
            start_step=start_step,
            algorithm=algorithm,
            retry_config=config,
            retry_stats=retry_stats,
        )
        timings.check_s += time.perf_counter() - check_start

    full_hhl = _fetch_hhl(run, timings, retry_config=config, retry_stats=retry_stats)
    selection = derive_vertical_level_selection(full_hhl, vertical_cutoff_m)
    half_level_height_m = full_hhl[selection.half_level_start :]
    step_chunks = _chunk_steps(list(range(start_step, run.max_step + 1)), chunk_size)
    chunk_count = len(step_chunks)
    static_fdb_request_count = 1 + (0 if start_step == 0 else 1) * (2 if algorithm == ALGORITHM_ICON else 1)
    dynamic_fdb_request_count = (4 if algorithm == ALGORITHM_ICON else 3) * chunk_count
    previous_total_precip: np.ndarray | None = (
        None
        if start_step == 0
        else _fetch_total_precip_step(
            run,
            step=start_step - 1,
            timings=timings,
            retry_config=config,
            retry_stats=retry_stats,
        )
    )
    previous_icon_accumulations: dict[str, np.ndarray] = (
        {}
        if algorithm != ALGORITHM_ICON or start_step == 0
        else _fetch_icon_microphysics_step(
            run,
            step=start_step - 1,
            timings=timings,
            retry_config=config,
            retry_stats=retry_stats,
        )
    )
    written = 0
    diagnostic_netcdf_outputs_written = 0
    active_columns = 0
    total_columns = 0
    data_quality = {key: 0 for key in DATA_QUALITY_KEYS}

    if not prefetch:
        for steps in step_chunks:
            chunk = _fetch_chunk(
                run,
                steps=steps,
                full_level_start=selection.full_level_start,
                algorithm=algorithm,
                timings=timings,
                retry_config=config,
                retry_stats=retry_stats,
            )
            previous_total_precip, chunk_written, chunk_diagnostic_netcdf_outputs, chunk_active, chunk_total, chunk_quality = _process_chunk(
                chunk,
                timings=timings,
                retained_full_levels=selection.retained_full_levels,
                half_level_height_m=half_level_height_m,
                previous_total_precip=previous_total_precip,
                previous_icon_accumulations=previous_icon_accumulations,
                output_root=output_root,
                run=run,
                output_model=output_model,
                precip_mask_threshold_mm=precip_mask_threshold_mm,
                algorithm=algorithm,
                vertical_cutoff_m=vertical_cutoff_m,
                write_probability_products=write_probability_products,
                output_format=normalized_output_format,
                retry_config=config,
                retry_stats=retry_stats,
            )
            written += chunk_written
            diagnostic_netcdf_outputs_written += chunk_diagnostic_netcdf_outputs
            active_columns += chunk_active
            total_columns += chunk_total
            for key in DATA_QUALITY_KEYS:
                data_quality[key] += int(chunk_quality.get(key, 0))
    else:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _fetch_chunk,
                run,
                steps=step_chunks[0],
                full_level_start=selection.full_level_start,
                algorithm=algorithm,
                retry_config=config,
            )
            for index, expected_steps in enumerate(step_chunks):
                chunk = future.result()
                if chunk.retry_stats:
                    retry_stats.add_mapping(chunk.retry_stats)
                _record_chunk_request_timings(timings, chunk)
                if chunk.steps != expected_steps:
                    raise RuntimeError(f"Fetched steps {chunk.steps}, expected {expected_steps}")
                if index + 1 < len(step_chunks):
                    future = executor.submit(
                        _fetch_chunk,
                        run,
                        steps=step_chunks[index + 1],
                        full_level_start=selection.full_level_start,
                        algorithm=algorithm,
                        retry_config=config,
                    )
                previous_total_precip, chunk_written, chunk_diagnostic_netcdf_outputs, chunk_active, chunk_total, chunk_quality = _process_chunk(
                    chunk,
                    timings=timings,
                    retained_full_levels=selection.retained_full_levels,
                    half_level_height_m=half_level_height_m,
                    previous_total_precip=previous_total_precip,
                    previous_icon_accumulations=previous_icon_accumulations,
                    output_root=output_root,
                    run=run,
                    output_model=output_model,
                    precip_mask_threshold_mm=precip_mask_threshold_mm,
                    algorithm=algorithm,
                    vertical_cutoff_m=vertical_cutoff_m,
                    write_probability_products=write_probability_products,
                    output_format=normalized_output_format,
                    retry_config=config,
                    retry_stats=retry_stats,
                )
                written += chunk_written
                diagnostic_netcdf_outputs_written += chunk_diagnostic_netcdf_outputs
                active_columns += chunk_active
                total_columns += chunk_total
                for key in DATA_QUALITY_KEYS:
                    data_quality[key] += int(chunk_quality.get(key, 0))

    LOGGER.info(
        "finished member=%s written=%s active_columns=%s total_columns=%s wall_s=%.3f",
        run.member,
        written,
        active_columns,
        total_columns,
        time.perf_counter() - wall_start,
    )

    return {
        "run": {
            "date": run.date,
            "time": run.time,
            "model": run.model,
            "member": run.member,
            "type": run.type,
            "number": run.number,
            "max_step": run.max_step,
        },
        "chunk_size": chunk_size,
        "prefetch": prefetch,
        "start_step": start_step,
        "algorithm": algorithm,
        "steps": sum(len(chunk) for chunk in step_chunks),
        "chunks": chunk_count,
        "static_fdb_request_count": static_fdb_request_count,
        "dynamic_fdb_request_count": dynamic_fdb_request_count,
        "fdb_request_count": static_fdb_request_count + dynamic_fdb_request_count,
        "fdb_utils_check_count": (
            3 + len(ML_PARAMS) + (len(ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS) if algorithm == ALGORITHM_ICON else 0)
            if check_inputs
            else 0
        ),
        "written": written,
        "newly_written": written,
        "reused": 0,
        "resumed": False,
        "diagnostic_netcdf_outputs_written": diagnostic_netcdf_outputs_written,
        "retained_full_levels": selection.retained_full_levels,
        "active_columns": active_columns,
        "total_columns": total_columns,
        "data_quality": data_quality,
        "retry_stats": retry_stats.as_dict(),
        "timings_s": timings.as_dict(),
        "wall_s": round(time.perf_counter() - wall_start, 3),
    }


def _process_member(
    *,
    model: str,
    member: str,
    date: str,
    time_value: str,
    start_step: int,
    max_step: int,
    output_root: Path,
    chunk_size: int,
    prefetch: bool,
    check_inputs: bool,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float,
    algorithm: str = ALGORITHM_FIRDEWSA,
    write_probability_products: bool = False,
    output_format: str = "grib2",
    retry_config: RetryConfig | None = None,
    resume: bool = True,
) -> dict[str, object]:
    return process_member_run(
        run=_make_run(model, member, date, time_value, max_step),
        output_root=output_root,
        output_model=model,
        chunk_size=chunk_size,
        prefetch=prefetch,
        check_inputs=check_inputs,
        precip_mask_threshold_mm=precip_mask_threshold_mm,
        vertical_cutoff_m=vertical_cutoff_m,
        algorithm=algorithm,
        start_step=start_step,
        write_probability_products=write_probability_products,
        output_format=output_format,
        retry_config=retry_config,
        resume=resume,
    )


def _merge_timings(results: Iterable[dict[str, object]]) -> dict[str, float]:
    merged = {key: 0.0 for key in TIMING_KEYS}
    for result in results:
        timings = result.get("timings_s")
        if not isinstance(timings, dict):
            continue
        for key in TIMING_KEYS:
            merged[key] += float(timings.get(key, 0.0))
    return {key: round(value, 3) for key, value in merged.items()}


def _merge_data_quality(results: Iterable[dict[str, object]]) -> dict[str, int]:
    merged = {key: 0 for key in DATA_QUALITY_KEYS}
    for result in results:
        quality = result.get("data_quality")
        if not isinstance(quality, dict):
            continue
        for key in DATA_QUALITY_KEYS:
            merged[key] += int(quality.get(key, 0))
    return merged


def _merge_retry_stats(
    results: Iterable[dict[str, object]],
    *,
    extra: RetryStats | None = None,
    failed_retry_stats: Iterable[dict[str, int]] = (),
) -> dict[str, int]:
    merged = RetryStats()
    if extra is not None:
        merged.add(extra)
    for raw_failed_stats in failed_retry_stats:
        merged.add_mapping(raw_failed_stats)
    for result in results:
        raw_stats = result.get("retry_stats")
        if not isinstance(raw_stats, dict):
            continue
        merged.add_mapping({str(key): int(value) for key, value in raw_stats.items()})
    return merged.as_dict()


def _failure_category(exc: Exception) -> str:
    if isinstance(exc, FdbIncompleteError):
        return "fdb_incomplete"
    if isinstance(exc, FdbTransientError):
        return "fdb_transient_exhausted"
    if isinstance(exc, (OSError, PermissionError)):
        return "output_write_failed"
    if isinstance(exc, (ValueError, KeyError, RuntimeError)):
        message = str(exc).lower()
        if "non-finite" in message or "invalid" in message or "shape" in message:
            return "data_quality_failed"
    return "member_failed"


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


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _write_run_marker(run_dir: Path, name: str, payload: dict[str, object]) -> Path:
    marker_payload = {
        **payload,
        "status": name.lower(),
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    return _atomic_write_json(marker_payload, run_dir / f"{name}.json")


def _ensure_run_contract(
    run_dir: Path,
    *,
    model: str,
    date: str,
    time_value: str,
    algorithm: str,
    output_format: str,
    precip_mask_threshold_mm: float,
    vertical_cutoff_m: float,
    write_probability_products: bool,
) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": 1,
        "model": model,
        "date": date,
        "time": time_value,
        "diagnostic_algorithm": algorithm,
        "output_format": output_format,
        "precip_mask_threshold_mm": precip_mask_threshold_mm,
        "vertical_cutoff_m": vertical_cutoff_m,
        "write_probability_products": write_probability_products,
        "member_contract": list(MODEL_MEMBERS[model]),
    }
    path = run_dir / "CONTRACT.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Cannot read existing run contract {path}: {exc}") from exc
        if existing != contract:
            raise RuntimeError(
                f"Output cycle contract mismatch at {path}; use a separate output root for a different algorithm or format"
            )
        return contract

    existing_outputs = any(run_dir.glob("[0-9][0-9][0-9]/lfff*.ptype.*")) or (run_dir / "probabilities").exists()
    if existing_outputs:
        raise RuntimeError(
            f"Refusing to adopt uncontracted existing outputs in {run_dir}; move them aside or use a separate output root"
        )
    _atomic_write_json(contract, path)
    return contract


def _finalize_failed_run_marker(
    func: Callable[..., dict[str, object]],
) -> Callable[..., dict[str, object]]:
    @wraps(func)
    def wrapper(*args: object, **kwargs: object) -> dict[str, object]:
        token = _ACTIVE_RUN_MARKER.set(None)
        lock_token = _ACTIVE_CYCLE_LOCK.set(None)
        try:
            return func(*args, **kwargs)
        except BaseException as exc:
            active_marker = _ACTIVE_RUN_MARKER.get()
            if active_marker is not None:
                run_dir, marker_base = active_marker
                failure_payload = {
                    **marker_base,
                    "monitoring_status": "critical",
                    "monitoring_ok": False,
                    "recommended_exit_code": 1,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                try:
                    _write_run_marker(run_dir, "FAILED", failure_payload)
                    _safe_unlink(run_dir / "DONE.json")
                except Exception:
                    LOGGER.exception("failed to write terminal run marker after unhandled error")
                finally:
                    _safe_unlink(run_dir / "RUNNING.json")
            raise
        finally:
            active_lock = _ACTIVE_CYCLE_LOCK.get()
            if active_lock is not None:
                active_lock.release()
            _ACTIVE_RUN_MARKER.reset(token)
            _ACTIVE_CYCLE_LOCK.reset(lock_token)

    return wrapper


def _collect_run_metadata(
    *,
    model: str,
    date: str,
    time_value: str,
    run_id: str | None,
    event_id: str | None,
    attempt: int | None,
    output_format: str,
    write_probability_products: bool,
    algorithm: str,
) -> dict[str, object]:
    scheduler: dict[str, object] = {}
    for key in ("SLURM_JOB_ID", "SLURM_JOB_NAME", "SLURM_JOB_PARTITION", "SLURM_SUBMIT_HOST"):
        value = os.environ.get(key)
        if value:
            scheduler[key.lower()] = value
    return {
        "run_id": run_id or f"{model}-{date}-{time_value}",
        "event_id": event_id,
        "attempt": attempt,
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
        "pid": os.getpid(),
        "scheduler": scheduler,
        "product_mode": {
            "output_format": output_format,
            "write_probability_products": write_probability_products,
            "diagnostic_algorithm": algorithm,
        },
    }


def _algorithm_fidelity(algorithm: str, model: str) -> dict[str, object]:
    spec = MODEL_SPECS[model]
    if algorithm == ALGORITHM_FIRDEWSA:
        return {
            "mode": "original_firdewsa",
            "reference": "Zukanovic MSc thesis implementation",
            "online_alignment": "not_applicable",
            "fdb_view": spec.uenv_view,
        }
    return {
        "mode": "icon_adapted_partial_archive",
        "reference_commit": ICON_REFERENCE_COMMIT,
        "thermodynamic_core": "matched_to_fortran_reference",
        "available_archived_accumulations": [name.lower() for name in ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS],
        "unavailable_online_rate_components": [name.lower() for name in ICON_UNAVAILABLE_ONLINE_RATE_COMPONENTS],
        "surface_rate_derivation": "hourly_difference_of_archived_accumulations_kg_m-2_s-1",
        "exact_online_refinement_parity": False,
        "fdb_view": spec.uenv_view,
        "limitation": "convective rain/snow and hail rates used online are not archived in the selected FDB dataset",
    }


@_finalize_failed_run_marker
def run_operational(
    *,
    model: str,
    output_root: Path | None = None,
    algorithm: str = ALGORITHM_FIRDEWSA,
    members: tuple[str, ...] | None = None,
    date: str | None = None,
    time_value: str | None = None,
    start_step: int = 1,
    max_step: int | None = None,
    lookback_days: int = 2,
    chunk_size: int = 2,
    workers: int | None = None,
    prefetch: bool = True,
    check_inputs: bool = True,
    precip_mask_threshold_mm: float | None = None,
    vertical_cutoff_m: float = DEFAULT_VERTICAL_CUTOFF_M,
    summary_json: Path | None = None,
    monitoring_json: Path | None = None,
    max_wall_s: float | None = None,
    check_output_files: bool = False,
    write_probability_products: bool = False,
    output_format: str = "grib2",
    run_id: str | None = None,
    event_id: str | None = None,
    attempt: int | None = None,
    fdb_retries: int = 0,
    fdb_retry_initial_s: float = 10.0,
    fdb_retry_max_s: float = 120.0,
    resume: bool = True,
    lock_timeout_s: float = 0.0,
) -> dict[str, object]:
    if (date is None) != (time_value is None):
        raise ValueError("date and time_value must be provided together")
    if date is not None and time_value is not None:
        validate_run_date_time(date, time_value)
    if max_wall_s is not None and max_wall_s <= 0:
        raise ValueError(f"max_wall_s must be positive, got {max_wall_s}")
    if attempt is not None and attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    if lock_timeout_s < 0:
        raise ValueError(f"lock_timeout_s must be non-negative, got {lock_timeout_s}")
    retry_config = retry_config_from_options(
        retries=fdb_retries,
        initial_delay_s=fdb_retry_initial_s,
        max_delay_s=fdb_retry_max_s,
    )
    discovery_retry_stats = RetryStats()
    normalized_output_format = normalize_output_format(output_format)
    if write_probability_products and normalized_output_format != "netcdf":
        raise ValueError("--write-probability-products requires output_format='netcdf'")

    LOGGER.info("starting operational run model=%s members=%s", model, members if members is not None else "all")
    cycle_max_step = max_step
    if cycle_max_step is None and time_value is not None:
        cycle_max_step = expected_cycle_max_step(model, time_value)
    config = config_for_model(
        model,
        members=members,
        output_root=output_root,
        algorithm=algorithm,
        precip_mask_threshold_mm=precip_mask_threshold_mm,
        vertical_cutoff_m=vertical_cutoff_m,
        start_step=start_step,
        max_step=cycle_max_step,
        workers=workers,
        chunk_size=chunk_size,
        prefetch=prefetch,
        output_format=normalized_output_format,
    )
    spec = MODEL_SPECS[model]
    if spec.requires_explicit_cycle and (date is None or time_value is None):
        raise ValueError(f"{model} requires an explicit --date and --time={spec.fixed_time}")
    if spec.fixed_time is not None and time_value is not None and time_value != spec.fixed_time:
        raise ValueError(f"{model} is a daily 00 UTC dataset; expected time_value={spec.fixed_time}, got {time_value}")
    _configure_meteoswiss_definitions()
    if config.algorithm == ALGORITHM_ICON:
        _warm_diagnostic(config.algorithm)
    else:
        _warm_diagnostic()

    discovery_s = 0.0
    if date is None or time_value is None:
        discovery_member = "000" if "000" in config.members else config.members[0]
        discovered = discover_complete_run(
            model=model,
            member=discovery_member,
            start_step=config.start_step,
            max_step=config.max_step,
            lookback_days=lookback_days,
            algorithm=config.algorithm,
            retry_config=retry_config,
            retry_stats=discovery_retry_stats,
        )
        date = discovered.date
        time_value = discovered.time
        discovery_s = discovered.discovery_s
        LOGGER.info("discovered run model=%s date=%s time=%s", model, date, time_value)

    run_dir = config.output_root / model / str(date) / str(time_value)
    cycle_lock = CycleLock(run_dir / ".cycle.lock", timeout_s=lock_timeout_s)
    cycle_lock.acquire()
    _ACTIVE_CYCLE_LOCK.set(cycle_lock)
    run_contract = _ensure_run_contract(
        run_dir,
        model=model,
        date=str(date),
        time_value=str(time_value),
        algorithm=config.algorithm,
        output_format=config.output_format,
        precip_mask_threshold_mm=config.precip_mask_threshold_mm,
        vertical_cutoff_m=config.vertical_cutoff_m,
        write_probability_products=write_probability_products,
    )
    run_metadata = _collect_run_metadata(
        model=model,
        date=str(date),
        time_value=str(time_value),
        run_id=run_id,
        event_id=event_id,
        attempt=attempt,
        output_format=config.output_format,
        write_probability_products=write_probability_products,
        algorithm=config.algorithm,
    )
    marker_base: dict[str, object] = {
        "model": model,
        "date": str(date),
        "time": str(time_value),
        "run_metadata": run_metadata,
    }
    _safe_unlink(run_dir / "DONE.json")
    _safe_unlink(run_dir / "FAILED.json")
    _write_run_marker(run_dir, "RUNNING", marker_base)
    _ACTIVE_RUN_MARKER.set((run_dir, marker_base))

    start = time.perf_counter()
    results: dict[str, dict[str, object]] = {}
    failed: dict[str, str] = {}
    failed_categories: dict[str, str] = {}
    failed_retry_stats: dict[str, dict[str, int]] = {}
    worker_count = min(config.max_workers, len(config.members))
    kwargs_by_member: dict[str, MemberProcessKwargs] = {
        member: {
            "model": model,
            "member": member,
            "date": date,
            "time_value": time_value,
            "start_step": config.start_step,
            "max_step": config.max_step,
            "output_root": config.output_root,
            "chunk_size": config.chunk_size,
            "prefetch": config.prefetch,
            "check_inputs": check_inputs,
            "precip_mask_threshold_mm": config.precip_mask_threshold_mm,
            "vertical_cutoff_m": config.vertical_cutoff_m,
            "algorithm": config.algorithm,
            "write_probability_products": write_probability_products,
            "output_format": config.output_format,
            "retry_config": retry_config,
            "resume": resume,
        }
        for member in config.members
    }

    if worker_count == 1:
        for member, kwargs in kwargs_by_member.items():
            try:
                results[member] = _process_member(**kwargs)
            except Exception as exc:
                failed[member] = f"{type(exc).__name__}: {exc}"
                failed_categories[member] = _failure_category(exc)
                if isinstance(exc, FdbTransientError):
                    failed_retry_stats[member] = exc.retry_stats
                LOGGER.exception("member=%s failed", member)
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(_process_member, **kwargs): member for member, kwargs in kwargs_by_member.items()}
            for future in as_completed(futures):
                member = futures[future]
                try:
                    results[member] = future.result()
                except Exception as exc:
                    failed[member] = f"{type(exc).__name__}: {exc}"
                    failed_categories[member] = _failure_category(exc)
                    if isinstance(exc, FdbTransientError):
                        failed_retry_stats[member] = exc.retry_stats
                    LOGGER.exception("member=%s failed", member)

    ordered_results = {member: results[member] for member in config.members if member in results}
    processed_members = tuple(ordered_results)
    if write_probability_products:
        LOGGER.info(
            "starting probability products model=%s date=%s time=%s members=%s failed_members=%s",
            model,
            date,
            time_value,
            len(config.members),
            len(failed),
        )
        probability_summary = generate_probability_products(
            output_root=config.output_root,
            model=model,
            date=str(date),
            time_value=str(time_value),
            members=config.members,
            processed_members=processed_members,
            failed_members=tuple(sorted(failed)),
            max_step=config.max_step,
            start_step=config.start_step,
            algorithm=config.algorithm,
        )
        LOGGER.info(
            "finished probability products model=%s date=%s time=%s status=%s files_written=%s",
            model,
            date,
            time_value,
            probability_summary.get("status"),
            probability_summary.get("files_written"),
        )
    else:
        probability_summary = disabled_probability_summary(
            config.output_root,
            model,
            str(date),
            str(time_value),
            config.members,
            algorithm=config.algorithm,
        )
    summary: dict[str, object] = {
        "model": model,
        "fdb_model": MODEL_TO_FDB[model],
        "fdb_source": {
            "uenv_view": spec.uenv_view,
            "class": spec.fdb_class,
            "stream": spec.stream,
            "expver": spec.expver,
            "fixed_time": spec.fixed_time,
            "accumulation_contract": spec.accumulation_contract,
        },
        "date": date,
        "time": time_value,
        "members": list(config.members),
        "processed_members": list(processed_members),
        "failed": failed,
        "failed_categories": failed_categories,
        "failure_categories": sorted(set(failed_categories.values())),
        "start_step": config.start_step,
        "max_step": config.max_step,
        "chunk_size": config.chunk_size,
        "prefetch": config.prefetch,
        "workers": worker_count,
        "output_root": str(config.output_root),
        "output_format": config.output_format,
        "diagnostic_algorithm": config.algorithm,
        "algorithm_fidelity": _algorithm_fidelity(config.algorithm, model),
        "precip_mask_threshold_mm": config.precip_mask_threshold_mm,
        "vertical_cutoff_m": config.vertical_cutoff_m,
        "discovery_s": round(discovery_s, 3),
        "timings_s": _merge_timings(ordered_results.values()),
        "retry_policy": {
            "fdb_retries": retry_config.retries,
            "fdb_retry_initial_s": retry_config.initial_delay_s,
            "fdb_retry_max_s": retry_config.max_delay_s,
        },
        "retry_stats": _merge_retry_stats(
            ordered_results.values(),
            extra=discovery_retry_stats,
            failed_retry_stats=failed_retry_stats.values(),
        ),
        "failed_retry_stats": failed_retry_stats,
        "data_quality": _merge_data_quality(ordered_results.values()),
        "probabilistic_products": probability_summary,
        "provenance": collect_runtime_provenance(),
        "run_metadata": run_metadata,
        "run_contract": run_contract,
        "resume": resume,
        "wall_s": round(time.perf_counter() - start, 3),
        "per_member": ordered_results,
    }
    monitoring = build_monitoring_status(
        summary,
        max_wall_s=max_wall_s,
        check_output_files=check_output_files,
    )
    summary["monitoring"] = monitoring

    default_summary = config.output_root / model / str(date) / str(time_value) / "summary.json"
    default_monitoring = config.output_root / model / str(date) / str(time_value) / "monitoring.json"
    _atomic_write_json(summary, default_summary)
    _atomic_write_json(monitoring, default_monitoring)
    if summary_json is not None:
        _atomic_write_json(summary, summary_json)
    if monitoring_json is not None:
        _atomic_write_json(monitoring, monitoring_json)
    marker_payload = {
        **marker_base,
        "monitoring_status": monitoring["status"],
        "monitoring_ok": monitoring["ok"],
        "recommended_exit_code": monitoring["recommended_exit_code"],
        "summary_json": str(default_summary),
        "monitoring_json": str(default_monitoring),
    }
    if monitoring["ok"]:
        _write_run_marker(run_dir, "DONE", marker_payload)
        _safe_unlink(run_dir / "FAILED.json")
    else:
        _write_run_marker(run_dir, "FAILED", marker_payload)
        _safe_unlink(run_dir / "DONE.json")
    _safe_unlink(run_dir / "RUNNING.json")
    LOGGER.info(
        "finished operational run model=%s processed=%s failed=%s monitoring_status=%s wall_s=%.3f",
        model,
        len(ordered_results),
        len(failed),
        monitoring["status"],
        summary["wall_s"],
    )
    return summary
