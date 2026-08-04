"""Cycle-aware ICON-REA-L-CH1 inventory and backfill orchestration."""

from __future__ import annotations

import argparse
import json
import os
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .constants import ALGORITHM_FIRDEWSA, DIAGNOSTIC_ALGORITHMS, INPUT_PARAM_IDS
from .operational import (
    MODEL_MEMBERS,
    MODEL_SPECS,
    RetryConfig,
    RetryStats,
    _all_member_outputs_valid,
    _atomic_write_json,
    _fdb_utils_list,
    _filter_parts,
    _make_run,
    retry_config_from_options,
    run_operational,
    validate_run_date_time,
)

REA_MODEL = "ICON-REA-L-CH1"
MANIFEST_SCHEMA_VERSION = 1


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


def list_available_reanalysis_dates(
    *,
    algorithm: str = ALGORITHM_FIRDEWSA,
    start_date: str | None = None,
    end_date: str | None = None,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> set[str]:
    """Return daily cycles present for every field required by the algorithm."""

    if algorithm not in DIAGNOSTIC_ALGORITHMS:
        raise ValueError(f"Unsupported algorithm {algorithm!r}")
    if (start_date is None) != (end_date is None):
        raise ValueError("start_date and end_date must be provided together")
    date_filter: str | None = None
    if start_date is not None and end_date is not None:
        _date_range(start_date, end_date)
        date_filter = start_date if start_date == end_date else f"{start_date}/to/{end_date}"
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    fields = [(name, "ml") for name in ("T", "P", "QV", "HHL")]
    fields.extend((name, "sfc") for name in ("TOT_PREC", "T_G"))
    if algorithm == "icon":
        fields.extend((name, "sfc") for name in ("RAIN_GSP", "SNOW_GSP", "GRAU_GSP"))
    dates_by_field: list[set[str]] = []
    for name, levtype in fields:
        values = _fdb_utils_list(
            _filter_parts(
                model=REA_MODEL,
                member="000",
                date=date_filter,
                time_value="0000",
                param=INPUT_PARAM_IDS[name],
                levtype=levtype,
            ),
            show_keys=("date",),
            retry_config=config,
            retry_stats=stats,
        )
        dates_by_field.append({str(value) for value in values.get("date", [])})
    return set.intersection(*dates_by_field)


def build_backfill_manifest(
    *,
    start_date: str,
    end_date: str,
    output_root: Path,
    manifest_path: Path,
    algorithm: str = ALGORITHM_FIRDEWSA,
    available_dates: set[str] | None = None,
    allow_missing_dates: bool = False,
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
    inventory = (
        list_available_reanalysis_dates(
            algorithm=algorithm,
            start_date=start_date,
            end_date=end_date,
            retry_config=retry_config,
            retry_stats=retry_stats,
        )
        if available_dates is None
        else available_dates
    )
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
    manifest: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "mode": "rea_l_ch1_daily_backfill",
        "model": REA_MODEL,
        "diagnostic_algorithm": algorithm,
        "output_format": "grib2",
        "members": list(MODEL_MEMBERS[REA_MODEL]),
        "start_step": 1,
        "max_step": MODEL_SPECS[REA_MODEL].max_step,
        "cycle_time": "0000",
        "cycle_date_range": {"start": start_date, "end": end_date, "inclusive": True},
        "valid_time_coverage": {
            "first_interval_end": start_valid.isoformat(),
            "last_interval_end": end_valid.isoformat(),
            "note": "Cycle D step 24 is valid at D+1 00 UTC; dates in this manifest are cycle dates.",
        },
        "output_root": str(output_root.resolve()),
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
        },
        "cycles": [{"index": index, "date": date} for index, date in enumerate(selected_dates)],
    }
    _atomic_write_json(manifest, manifest_path)
    return manifest


def load_manifest(path: Path) -> dict[str, object]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION or manifest.get("mode") != "rea_l_ch1_daily_backfill":
        raise RuntimeError(f"Unsupported backfill manifest: {path}")
    if manifest.get("model") != REA_MODEL or manifest.get("cycle_time") != "0000":
        raise RuntimeError(f"Invalid REA-L-CH1 cycle contract in {path}")
    cycles = manifest.get("cycles")
    if not isinstance(cycles, list) or not cycles:
        raise RuntimeError(f"Manifest contains no cycles: {path}")
    return manifest


def _manifest_cycle(manifest: dict[str, object], index: int) -> dict[str, object]:
    cycles = manifest.get("cycles")
    if not isinstance(cycles, list) or not (0 <= index < len(cycles)):
        raise IndexError(f"Backfill index {index} is outside 0..{len(cycles) - 1 if isinstance(cycles, list) else -1}")
    cycle = cycles[index]
    if not isinstance(cycle, dict) or cycle.get("index") != index:
        raise RuntimeError(f"Invalid cycle entry at index {index}")
    return cycle


def _verified_day_complete(manifest: dict[str, object], date: str) -> bool:
    output_root = Path(str(manifest["output_root"]))
    algorithm = str(manifest["diagnostic_algorithm"])
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
    cycle = _manifest_cycle(manifest, index)
    date = str(cycle["date"])
    output_root = Path(str(manifest["output_root"]))
    algorithm = str(manifest["diagnostic_algorithm"])
    receipt_path = manifest_path.parent / "receipts" / f"{index:05d}-{date}.json"
    if _verified_day_complete(manifest, date):
        receipt = {
            "index": index,
            "date": date,
            "status": "verified_existing",
            "ok": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(receipt, receipt_path)
        return receipt

    attempt = 1
    if receipt_path.is_file():
        try:
            previous_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            attempt = int(previous_receipt.get("attempt", 0)) + 1
        except Exception:
            attempt = 1

    try:
        summary = run_operational(
            model=REA_MODEL,
            output_root=output_root,
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
            event_id=f"{manifest_path.name}:{index}",
            attempt=attempt,
            fdb_retries=fdb_retries,
            fdb_retry_initial_s=fdb_retry_initial_s,
            fdb_retry_max_s=fdb_retry_max_s,
            resume=True,
            lock_timeout_s=lock_timeout_s,
        )
    except Exception as exc:
        receipt = {
            "index": index,
            "date": date,
            "status": "critical",
            "ok": False,
            "attempt": attempt,
            "error": f"{type(exc).__name__}: {exc}",
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(receipt, receipt_path)
        raise
    monitoring = summary.get("monitoring", {})
    ok = isinstance(monitoring, dict) and monitoring.get("ok") is True
    receipt = {
        "index": index,
        "date": date,
        "status": "complete" if ok else "critical",
        "ok": ok,
        "attempt": attempt,
        "summary_json": str(output_root / REA_MODEL / date / "0000" / "summary.json"),
        "monitoring_json": str(output_root / REA_MODEL / date / "0000" / "monitoring.json"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(receipt, receipt_path)
    return receipt


def campaign_status(*, manifest_path: Path, verify_outputs: bool = False) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    output_root = Path(str(manifest["output_root"]))
    cycles = manifest["cycles"]
    if not isinstance(cycles, list):
        raise RuntimeError(f"Invalid cycles in {manifest_path}")
    complete: list[str] = []
    failed: list[str] = []
    pending: list[str] = []
    for cycle in cycles:
        if not isinstance(cycle, dict):
            raise RuntimeError(f"Invalid cycle entry in {manifest_path}")
        date = str(cycle["date"])
        run_dir = output_root / REA_MODEL / date / "0000"
        if (run_dir / "FAILED.json").is_file():
            failed.append(date)
        elif (run_dir / "DONE.json").is_file() and (not verify_outputs or _verified_day_complete(manifest, date)):
            complete.append(date)
        else:
            pending.append(date)
    status: dict[str, object] = {
        "schema_version": 1,
        "manifest_path": str(manifest_path.resolve()),
        "model": REA_MODEL,
        "total_cycles": len(cycles),
        "complete_cycles": len(complete),
        "failed_cycles": len(failed),
        "pending_cycles": len(pending),
        "complete": not failed and not pending,
        "verified_outputs": verify_outputs,
        "failed_dates": failed,
        "pending_dates": pending,
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
    cycles = manifest["cycles"]
    if not isinstance(cycles, list):
        raise RuntimeError(f"Invalid cycles in {manifest_path}")
    if concurrency <= 0:
        raise ValueError(f"concurrency must be positive, got {concurrency}")
    if not partition.startswith("pp-"):
        raise ValueError(f"CPU backfills require a pp-* partition, got {partition!r}")
    repo_root = Path(__file__).resolve().parents[2]
    quoted_repo_root = shlex.quote(str(repo_root))
    quoted_manifest = shlex.quote(str(manifest_path.resolve()))
    text = f"""#!/usr/bin/env bash
#SBATCH --job-name=ptype-rea
#SBATCH --partition={partition}
#SBATCH --time={wall_time}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --array=0-{len(cycles) - 1}%{concurrency}

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
    parser = argparse.ArgumentParser(description="Plan, run, and verify ICON-REA-L-CH1 daily backfills")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--start-date", required=True)
    plan.add_argument("--end-date", required=True)
    plan.add_argument("--output-root", type=Path, required=True)
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument("--algorithm", choices=DIAGNOSTIC_ALGORITHMS, default=ALGORITHM_FIRDEWSA)
    plan.add_argument("--allow-missing-dates", action="store_true")
    plan.add_argument("--slurm-script", type=Path)
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
    args = build_parser().parse_args()
    if args.command == "plan":
        manifest = build_backfill_manifest(
            start_date=args.start_date,
            end_date=args.end_date,
            output_root=args.output_root,
            manifest_path=args.manifest,
            algorithm=args.algorithm,
            allow_missing_dates=args.allow_missing_dates,
        )
        script_path = args.slurm_script or args.manifest.with_suffix(".sbatch")
        write_slurm_array_script(
            manifest_path=args.manifest,
            script_path=script_path,
            concurrency=args.concurrency,
            partition=args.partition,
            wall_time=args.wall_time,
        )
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
