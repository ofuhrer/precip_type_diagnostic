"""CLI entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .gribio import build_jobs, parse_hours, parse_members, process_job, report_for_run
from .operational import run_operational


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ICON thesis precipitation-type diagnostic")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input-run", help="Path to /.../icon run directory for debug/member-hour processing")
    input_group.add_argument(
        "--input-root",
        help="Path to the MeteoSwiss cache root, e.g. /opr/osm/inn/cache, for operational run processing",
    )
    parser.add_argument("--output-dir", required=False, help="Destination directory for debug/member-hour GRIB files")
    parser.add_argument("--output-root", required=False, help="Destination root for operational output products")
    parser.add_argument("--model", required=True, choices=["ICON-CH1-EPS", "ICON-CH2-EPS"])
    parser.add_argument("--members", default="all", help="'all', one member, or a comma-separated list")
    parser.add_argument("--hours", default="all", help="'all', one step like 00010000, or a comma-separated list")
    parser.add_argument("--run", default=None, help="Run id such as 26042318_741 or 'latest' for operational mode")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing valid outputs in operational mode",
    )
    parser.add_argument(
        "--summary-json",
        required=False,
        help="Optional extra summary JSON path for operational mode",
    )
    parser.add_argument(
        "--precip-mask-threshold-mm",
        type=float,
        default=None,
        help="Microphysics precipitation masking threshold in mm/h",
    )
    parser.add_argument("--report-only", action="store_true", help="Only inspect the input run")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        parsed_members = parse_members(args.members)
        parsed_hours = parse_hours(args.hours)
    except ValueError as exc:
        parser.error(str(exc))

    if args.input_run:
        input_run = Path(args.input_run)
        if args.report_only:
            print(json.dumps(report_for_run(input_run), indent=2, sort_keys=True))
            return 0

        if not args.output_dir:
            parser.error("--output-dir is required unless --report-only is used")

        jobs, skipped = build_jobs(
            input_run=input_run,
            members=parsed_members,
            hours=parsed_hours,
        )

        written = []
        for job in jobs:
            written.append(
                str(
                    process_job(
                        job,
                        Path(args.output_dir),
                        precip_mask_threshold_mm=args.precip_mask_threshold_mm or 0.0,
                    )
                )
            )

        print(json.dumps({"written": written, "skipped": skipped}, indent=2, sort_keys=True))
        return 0

    run_id = args.run or "latest"
    summary = run_operational(
        model=args.model,
        run=run_id,
        input_root=Path(args.input_root),
        output_root=Path(args.output_root) if args.output_root else None,
        precip_mask_threshold_mm=args.precip_mask_threshold_mm,
        overwrite=args.overwrite,
        summary_json=Path(args.summary_json) if args.summary_json else None,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
