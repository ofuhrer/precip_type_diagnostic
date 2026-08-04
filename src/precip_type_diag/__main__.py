"""CLI entry point for FDB-backed precipitation-type production."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from .constants import ALGORITHM_FIRDEWSA, DEFAULT_VERTICAL_CUTOFF_M, DIAGNOSTIC_ALGORITHMS
from .operational import (
    MODEL_MAX_STEP,
    MODEL_TO_FDB,
    OUTPUT_FORMATS,
    parse_members,
    run_operational,
    validate_run_date_time,
)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "process": record.process,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_logging(*, level: str, log_format: str, log_file: Path | None) -> None:
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Unsupported log level {level!r}")
    formatter: logging.Formatter
    if log_format == "json":
        formatter = JsonLogFormatter()
    elif log_format == "text":
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    else:
        raise ValueError(f"Unsupported log format {log_format!r}")
    handler: logging.Handler
    if log_file is None:
        handler = logging.StreamHandler()
    else:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ICON FDB precipitation-type diagnostic")
    parser.add_argument("--model", choices=sorted(MODEL_TO_FDB), required=True)
    parser.add_argument(
        "--algorithm",
        choices=DIAGNOSTIC_ALGORITHMS,
        default=ALGORITHM_FIRDEWSA,
        help="Scientific algorithm: preserve the original Firdewsa method (default) or use the ICON-adapted diagnostic.",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--members", default="all", help="Use 'all' or a comma-separated list like 000,001")
    parser.add_argument("--date", default=None, help="FDB run date YYYYMMDD. Default: discover latest complete run.")
    parser.add_argument("--time", dest="time_value", default=None, help="FDB run time HHMM. Default: discover latest complete run.")
    parser.add_argument("--start-step", type=int, default=1, help="First forecast step to diagnose. Default: 1 because step 0 has no previous hourly precipitation interval.")
    parser.add_argument("--max-step", type=int, default=None)
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--chunk-size", type=int, default=2)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--summary-json", type=Path, default=None)
    parser.add_argument("--monitoring-json", type=Path, default=None)
    parser.add_argument("--output-format", type=str.lower, choices=OUTPUT_FORMATS, default="grib2")
    parser.add_argument("--run-id", default=None, help="Optional production run identifier recorded in summary and markers")
    parser.add_argument("--event-id", default=None, help="Optional upstream notification/event identifier recorded in summary and markers")
    parser.add_argument("--attempt", type=int, default=None, help="Optional production attempt number recorded in summary and markers")
    parser.add_argument("--log-level", default="WARNING", help="Python logging level. Default: WARNING")
    parser.add_argument("--log-format", choices=("text", "json"), default="text", help="Log format. Default: text")
    parser.add_argument("--log-file", type=Path, default=None, help="Optional log file path")
    parser.add_argument("--fdb-retries", type=int, default=0, help="Additional retries for transient FDB list/retrieve/decode failures")
    parser.add_argument("--fdb-retry-initial-s", type=float, default=10.0, help="Initial FDB retry delay in seconds")
    parser.add_argument("--fdb-retry-max-s", type=float, default=120.0, help="Maximum FDB retry delay in seconds")
    parser.add_argument("--max-wall-s", type=float, default=None, help="Fail monitoring if run wall time exceeds this limit")
    parser.add_argument("--no-output-file-check", action="store_true", help="Skip post-run checks for expected member output files")
    parser.add_argument(
        "--write-probability-products",
        action="store_true",
        help="Write member diagnostic variables and strict all-member ensemble probability NetCDF products; requires --output-format=netcdf",
    )
    parser.add_argument("--no-prefetch", action="store_true", help="Disable chunk prefetching")
    parser.add_argument("--skip-input-checks", action="store_true", help="Skip FDB completeness checks")
    parser.add_argument(
        "--precip-mask-threshold-mm",
        type=float,
        default=None,
        help="Override the precipitation amount mask (default: 0.0 for Firdewsa, 0.01 for hourly ICON mode).",
    )
    parser.add_argument("--vertical-cutoff-m", type=float, default=DEFAULT_VERTICAL_CUTOFF_M)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (args.date is None) != (args.time_value is None):
        parser.error("--date and --time must be provided together")
    if args.date is not None and args.time_value is not None:
        try:
            validate_run_date_time(args.date, args.time_value)
        except ValueError as exc:
            parser.error(str(exc))
    if args.max_step is not None and args.max_step < 0:
        parser.error(f"--max-step must be non-negative, got {args.max_step}")
    if args.start_step < 0:
        parser.error(f"--start-step must be non-negative, got {args.start_step}")
    effective_max_step = MODEL_MAX_STEP[args.model] if args.max_step is None else args.max_step
    if args.start_step > effective_max_step:
        parser.error(f"--start-step must be <= --max-step, got start_step={args.start_step} max_step={effective_max_step}")
    if args.chunk_size <= 0:
        parser.error(f"--chunk-size must be positive, got {args.chunk_size}")
    if args.workers is not None and args.workers <= 0:
        parser.error(f"--workers must be positive, got {args.workers}")
    if args.max_wall_s is not None and args.max_wall_s <= 0:
        parser.error(f"--max-wall-s must be positive, got {args.max_wall_s}")
    if args.attempt is not None and args.attempt < 1:
        parser.error(f"--attempt must be >= 1, got {args.attempt}")
    if args.fdb_retries < 0:
        parser.error(f"--fdb-retries must be non-negative, got {args.fdb_retries}")
    if args.fdb_retry_initial_s <= 0:
        parser.error(f"--fdb-retry-initial-s must be positive, got {args.fdb_retry_initial_s}")
    if args.fdb_retry_max_s <= 0:
        parser.error(f"--fdb-retry-max-s must be positive, got {args.fdb_retry_max_s}")
    if args.fdb_retry_max_s < args.fdb_retry_initial_s:
        parser.error("--fdb-retry-max-s must be >= --fdb-retry-initial-s")
    if args.write_probability_products and args.output_format != "netcdf":
        parser.error("--write-probability-products requires --output-format=netcdf")

    try:
        members = parse_members(args.members, args.model)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        configure_logging(level=args.log_level, log_format=args.log_format, log_file=args.log_file)
    except ValueError as exc:
        parser.error(str(exc))

    summary = run_operational(
        model=args.model,
        algorithm=args.algorithm,
        output_root=args.output_root,
        members=members,
        date=args.date,
        time_value=args.time_value,
        start_step=args.start_step,
        max_step=effective_max_step,
        lookback_days=args.lookback_days,
        chunk_size=args.chunk_size,
        workers=args.workers,
        prefetch=not args.no_prefetch,
        check_inputs=not args.skip_input_checks,
        precip_mask_threshold_mm=args.precip_mask_threshold_mm,
        vertical_cutoff_m=args.vertical_cutoff_m,
        summary_json=args.summary_json,
        monitoring_json=args.monitoring_json,
        output_format=args.output_format,
        run_id=args.run_id,
        event_id=args.event_id,
        attempt=args.attempt,
        fdb_retries=args.fdb_retries,
        fdb_retry_initial_s=args.fdb_retry_initial_s,
        fdb_retry_max_s=args.fdb_retry_max_s,
        max_wall_s=args.max_wall_s,
        check_output_files=not args.no_output_file_check,
        write_probability_products=args.write_probability_products,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    monitoring = summary.get("monitoring", {})
    if isinstance(monitoring, dict):
        exit_code = int(monitoring.get("recommended_exit_code", 1 if summary["failed"] else 0))
    else:
        exit_code = 1 if summary["failed"] else 0
    logging.getLogger(__name__).info("finished CLI run exit_code=%s", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
