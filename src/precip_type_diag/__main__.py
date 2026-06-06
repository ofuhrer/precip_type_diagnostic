"""CLI entry point for FDB-backed precipitation-type production."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .constants import DEFAULT_VERTICAL_CUTOFF_M
from .operational import MODEL_MAX_STEP, MODEL_TO_FDB, parse_members, run_operational


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ICON FDB precipitation-type diagnostic")
    parser.add_argument("--model", choices=sorted(MODEL_TO_FDB), required=True)
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
    parser.add_argument("--max-wall-s", type=float, default=None, help="Fail monitoring if run wall time exceeds this limit")
    parser.add_argument("--no-output-file-check", action="store_true", help="Skip post-run checks for expected output GRIB files")
    parser.add_argument(
        "--write-probability-products",
        action="store_true",
        help="Write member diagnostic NetCDF sidecars and strict all-member ensemble probability NetCDF products",
    )
    parser.add_argument("--no-prefetch", action="store_true", help="Disable chunk prefetching")
    parser.add_argument("--skip-input-checks", action="store_true", help="Skip FDB completeness checks")
    parser.add_argument("--precip-mask-threshold-mm", type=float, default=None)
    parser.add_argument("--vertical-cutoff-m", type=float, default=DEFAULT_VERTICAL_CUTOFF_M)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if (args.date is None) != (args.time_value is None):
        parser.error("--date and --time must be provided together")
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

    try:
        members = parse_members(args.members, args.model)
    except ValueError as exc:
        parser.error(str(exc))

    summary = run_operational(
        model=args.model,
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
        max_wall_s=args.max_wall_s,
        check_output_files=not args.no_output_file_check,
        write_probability_products=args.write_probability_products,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    monitoring = summary.get("monitoring", {})
    if isinstance(monitoring, dict):
        return int(monitoring.get("recommended_exit_code", 1 if summary["failed"] else 0))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
