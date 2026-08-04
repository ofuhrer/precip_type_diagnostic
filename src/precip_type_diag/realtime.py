"""Progressive realtime-cycle orchestration for Balfrin FDB production."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .constants import ALGORITHM_FIRDEWSA, DIAGNOSTIC_ALGORITHMS, ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS, INPUT_PARAM_IDS
from .netcdfio import inspect_netcdf
from .operational import (
    FULL_LEVELS,
    HALF_LEVELS,
    ML_PARAMS,
    MODEL_MEMBERS,
    MODEL_SPECS,
    CycleLock,
    RetryConfig,
    RetryStats,
    _all_member_outputs_valid,
    _atomic_write_json,
    _fdb_utils_list,
    _filter_parts,
    _make_run,
    _parse_levels,
    _parse_steps,
    _step_expr,
    expected_cycle_max_step,
    normalize_algorithm,
    retry_config_from_options,
    run_operational,
    validate_run_date_time,
)
from .probabilities import probability_output_path, probability_product_names, step_token

LOGGER = logging.getLogger(__name__)
REALTIME_MODELS = ("ICON-CH1-EPS", "ICON-CH2-EPS")
CYCLE_STATE_NAME = "CYCLE.json"


def _listed_values(
    *,
    model: str,
    member: str,
    date: str,
    time_value: str,
    param: int,
    levtype: str,
    max_step: int,
    retry_config: RetryConfig,
    retry_stats: RetryStats,
) -> dict[str, list[object]]:
    return _fdb_utils_list(
        _filter_parts(
            model=model,
            member=member,
            date=date,
            time_value=time_value,
            param=param,
            levtype=levtype,
            step=_step_expr(list(range(0, max_step + 1))),
        ),
        show_keys=("step", "levelist", "timespan"),
        retry_config=retry_config,
        retry_stats=retry_stats,
    )


def _available_member_contiguous_step(
    *,
    model: str,
    member: str,
    date: str,
    time_value: str,
    algorithm: str = ALGORITHM_FIRDEWSA,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> int:
    """Return one member's latest contiguous diagnostic step."""

    algorithm = normalize_algorithm(algorithm)
    if model not in REALTIME_MODELS:
        raise ValueError(f"Progressive realtime processing supports only: {', '.join(REALTIME_MODELS)}")
    validate_run_date_time(date, time_value)
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    horizon = MODEL_SPECS[model].max_step

    hhl = _listed_values(
        model=model,
        member=member,
        date=date,
        time_value=time_value,
        param=INPUT_PARAM_IDS["HHL"],
        levtype="ml",
        max_step=0,
        retry_config=config,
        retry_stats=stats,
    )
    if 0 not in _parse_steps(hhl.get("step", [])) or not set(range(1, HALF_LEVELS + 1)).issubset(
        _parse_levels(hhl.get("levelist", []))
    ):
        return 0

    diagnostic_step_sets: list[set[int]] = []
    for param in (*ML_PARAMS, INPUT_PARAM_IDS["T_G"]):
        levtype = "ml" if param in ML_PARAMS else "sfc"
        values = _listed_values(
            model=model,
            member=member,
            date=date,
            time_value=time_value,
            param=param,
            levtype=levtype,
            max_step=horizon,
            retry_config=config,
            retry_stats=stats,
        )
        if levtype == "ml" and not set(range(1, FULL_LEVELS + 1)).issubset(_parse_levels(values.get("levelist", []))):
            return 0
        diagnostic_step_sets.append(_parse_steps(values.get("step", [])))

    accumulation_names = ["TOT_PREC"]
    if algorithm == "icon":
        accumulation_names.extend(ICON_ARCHIVED_MICROPHYSICS_ACCUMULATIONS)
    accumulation_step_sets: list[set[int]] = []
    for name in accumulation_names:
        values = _listed_values(
            model=model,
            member=member,
            date=date,
            time_value=time_value,
            param=INPUT_PARAM_IDS[name],
            levtype="sfc",
            max_step=horizon,
            retry_config=config,
            retry_stats=stats,
        )
        accumulation_step_sets.append(_parse_steps(values.get("step", [])))

    available = 0
    for step in range(1, horizon + 1):
        if not all(step in values for values in diagnostic_step_sets):
            break
        if not all(step in values and step - 1 in values for values in accumulation_step_sets):
            break
        available = step
    return available


def available_contiguous_step(
    *,
    model: str,
    date: str,
    time_value: str,
    algorithm: str = ALGORITHM_FIRDEWSA,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> int:
    """Return the latest contiguous hour complete for every model member."""

    if model not in REALTIME_MODELS:
        raise ValueError(f"Progressive realtime processing supports only: {', '.join(REALTIME_MODELS)}")
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    return min(
        _available_member_contiguous_step(
            model=model,
            member=member,
            date=date,
            time_value=time_value,
            algorithm=algorithm,
            retry_config=config,
            retry_stats=stats,
        )
        for member in MODEL_MEMBERS[model]
    )


def discover_latest_candidate_cycle(
    *,
    model: str,
    lookback_days: int = 2,
    retry_config: RetryConfig | None = None,
    retry_stats: RetryStats | None = None,
) -> tuple[str, str]:
    """Find the latest cycle that has any control-member precipitation metadata."""

    if model not in REALTIME_MODELS:
        raise ValueError(f"Progressive realtime processing supports only: {', '.join(REALTIME_MODELS)}")
    if lookback_days < 0:
        raise ValueError(f"lookback_days must be non-negative, got {lookback_days}")
    config = RetryConfig() if retry_config is None else retry_config
    stats = RetryStats() if retry_stats is None else retry_stats
    today = datetime.now(timezone.utc).date()
    candidates: list[tuple[str, str]] = []
    for offset in range(lookback_days + 1):
        date = (today - timedelta(days=offset)).strftime("%Y%m%d")
        values = _fdb_utils_list(
            _filter_parts(
                model=model,
                member="000",
                date=date,
                param=INPUT_PARAM_IDS["TOT_PREC"],
                levtype="sfc",
            ),
            show_keys=("time",),
            retry_config=config,
            retry_stats=stats,
        )
        candidates.extend((date, str(value)) for value in values.get("time", []))
    if not candidates:
        raise RuntimeError(f"No realtime FDB cycle found for {model} in the last {lookback_days} day(s)")
    return max(candidates)


def _valid_probability_output(
    path: Path,
    *,
    model: str,
    date: str,
    time_value: str,
    step: int,
    algorithm: str,
) -> bool:
    if not path.is_file():
        return False
    try:
        attrs, variables = inspect_netcdf(path)
        expected = {
            "model": model,
            "date": date,
            "time": time_value,
            "step": step,
            "diagnostic_algorithm": algorithm,
        }
        return all(str(attrs.get(key)) == str(value) for key, value in expected.items()) and set(
            probability_product_names(algorithm)
        ).issubset(variables)
    except Exception:
        LOGGER.warning("probability output failed verification: %s", path, exc_info=True)
        return False


def _state_completed_through(
    state: dict[str, object],
    *,
    output_root: Path,
    model: str,
    date: str,
    time_value: str,
    algorithm: str,
) -> int:
    raw_completed = state.get("completed_through", 0)
    if not isinstance(raw_completed, (int, str)):
        return 0
    completed = int(raw_completed)
    if completed <= 0:
        return 0
    for member in MODEL_MEMBERS[model]:
        member_dir = output_root / model / date / time_value / member
        if any(not (member_dir / f"lfff{step_token(step)}.ptype.nc").is_file() for step in range(1, completed + 1)):
            return 0
    if any(
        not probability_output_path(output_root, model, date, time_value, step).is_file()
        for step in range(1, completed + 1)
    ):
        return 0
    if not all(
        _all_member_outputs_valid(
            run=_make_run(model, member, date, time_value, completed),
            output_root=output_root,
            output_model=model,
            start_step=completed,
            algorithm=algorithm,
            output_format="netcdf",
            require_diagnostics=True,
        )
        for member in MODEL_MEMBERS[model]
    ):
        return 0
    if not _valid_probability_output(
        probability_output_path(output_root, model, date, time_value, completed),
        model=model,
        date=date,
        time_value=time_value,
        step=completed,
        algorithm=algorithm,
    ):
        return 0
    return completed


def _load_state(path: Path, expected: dict[str, object]) -> dict[str, object]:
    if not path.exists():
        return {**expected, "completed_through": 0, "increments": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    for key, value in expected.items():
        if state.get(key) != value:
            raise RuntimeError(f"Cycle-state contract mismatch for {key!r} at {path}")
    if not isinstance(state.get("increments", []), list):
        raise RuntimeError(f"Invalid increments in {path}")
    return state


def run_progressive_cycle(
    *,
    model: str,
    output_root: Path,
    date: str | None = None,
    time_value: str | None = None,
    algorithm: str = ALGORITHM_FIRDEWSA,
    lookback_days: int = 2,
    workers: int = 8,
    chunk_size: int = 2,
    fdb_retries: int = 3,
    fdb_retry_initial_s: float = 10.0,
    fdb_retry_max_s: float = 120.0,
    lock_timeout_s: float = 0.0,
    through_step: int | None = None,
) -> dict[str, object]:
    algorithm = normalize_algorithm(algorithm)
    if model not in REALTIME_MODELS:
        raise ValueError(f"Progressive realtime processing supports only: {', '.join(REALTIME_MODELS)}")
    if (date is None) != (time_value is None):
        raise ValueError("date and time_value must be provided together")
    retry_config = retry_config_from_options(
        retries=fdb_retries,
        initial_delay_s=fdb_retry_initial_s,
        max_delay_s=fdb_retry_max_s,
    )
    retry_stats = RetryStats()
    if date is None or time_value is None:
        date, time_value = discover_latest_candidate_cycle(
            model=model,
            lookback_days=lookback_days,
            retry_config=retry_config,
            retry_stats=retry_stats,
        )
    validate_run_date_time(date, time_value)
    horizon = expected_cycle_max_step(model, time_value)
    if through_step is not None and not 1 <= through_step <= horizon:
        raise ValueError(f"through_step must be between 1 and {horizon}, got {through_step}")
    run_dir = output_root / model / date / time_value
    orchestration_lock = CycleLock(run_dir / ".progressive.lock", timeout_s=lock_timeout_s)
    orchestration_lock.acquire()
    state_path: Path | None = None
    state: dict[str, object] | None = None
    try:
        state_path = run_dir / CYCLE_STATE_NAME
        expected_state = {
            "schema_version": 1,
            "mode": "realtime_progressive",
            "model": model,
            "date": date,
            "time": time_value,
            "diagnostic_algorithm": algorithm,
            "output_format": "netcdf",
            "write_probability_products": True,
            "horizon": horizon,
            "members": list(MODEL_MEMBERS[model]),
        }
        state = _load_state(state_path, expected_state)
        state.pop("last_error", None)
        completed = _state_completed_through(
            state,
            output_root=output_root,
            model=model,
            date=date,
            time_value=time_value,
            algorithm=algorithm,
        )
        discovered_available = available_contiguous_step(
            model=model,
            date=date,
            time_value=time_value,
            algorithm=algorithm,
            retry_config=retry_config,
            retry_stats=retry_stats,
        )
        available = min(discovered_available, through_step) if through_step is not None else discovered_available
        state.update(
            {
                "completed_through": completed,
                "available_through": available,
                "discovered_available_through": discovered_available,
                "status": "complete" if completed == horizon else "ingesting",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "retry_stats": retry_stats.as_dict(),
            }
        )
        if available <= completed:
            _atomic_write_json(state, state_path)
            return state

        start_step = completed + 1
        increment_dir = run_dir / "increments"
        label = f"steps-{start_step:03d}-{available:03d}"
        summary_path = increment_dir / f"{label}.summary.json"
        monitoring_path = increment_dir / f"{label}.monitoring.json"
        summary = run_operational(
            model=model,
            output_root=output_root,
            algorithm=algorithm,
            members=MODEL_MEMBERS[model],
            date=date,
            time_value=time_value,
            start_step=start_step,
            max_step=available,
            chunk_size=chunk_size,
            workers=workers,
            summary_json=summary_path,
            monitoring_json=monitoring_path,
            check_output_files=True,
            write_probability_products=True,
            output_format="netcdf",
            run_id=f"{model}-{date}-{time_value}-{start_step:03d}-{available:03d}",
            fdb_retries=fdb_retries,
            fdb_retry_initial_s=fdb_retry_initial_s,
            fdb_retry_max_s=fdb_retry_max_s,
            resume=True,
            lock_timeout_s=lock_timeout_s,
        )
        monitoring = summary.get("monitoring", {})
        ok = isinstance(monitoring, dict) and monitoring.get("ok") is True
        raw_increments = state.get("increments", [])
        if not isinstance(raw_increments, list):
            raise RuntimeError(f"Invalid increments in {state_path}")
        increments = list(raw_increments)
        increments.append(
            {
                "start_step": start_step,
                "max_step": available,
                "ok": ok,
                "summary_json": str(summary_path),
                "monitoring_json": str(monitoring_path),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        state.update(
            {
                "completed_through": available if ok else completed,
                "available_through": available,
                "status": (
                    "critical"
                    if not ok
                    else "complete"
                    if available == horizon
                    else "ingesting"
                ),
                "increments": increments,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "last_monitoring": monitoring,
            }
        )
        _atomic_write_json(state, state_path)
        return state
    except Exception as exc:
        if state_path is not None and state is not None:
            state.update(
                {
                    "status": "critical",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
            )
            try:
                _atomic_write_json(state, state_path)
            except Exception:
                LOGGER.exception("failed to write critical progressive state: %s", state_path)
        raise
    finally:
        orchestration_lock.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Progressively publish an ingesting ICON EPS cycle")
    parser.add_argument("--model", choices=REALTIME_MODELS, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--date")
    parser.add_argument("--time", dest="time_value")
    parser.add_argument("--algorithm", choices=DIAGNOSTIC_ALGORITHMS, default=ALGORITHM_FIRDEWSA)
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--chunk-size", type=int, default=2)
    parser.add_argument("--fdb-retries", type=int, default=3)
    parser.add_argument("--fdb-retry-initial-s", type=float, default=10.0)
    parser.add_argument("--fdb-retry-max-s", type=float, default=120.0)
    parser.add_argument("--lock-timeout-s", type=float, default=0.0)
    parser.add_argument(
        "--through-step",
        type=int,
        help="Bound this invocation to a step already present in FDB (controlled catch-up and acceptance testing)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    state = run_progressive_cycle(
        model=args.model,
        output_root=args.output_root,
        date=args.date,
        time_value=args.time_value,
        algorithm=args.algorithm,
        lookback_days=args.lookback_days,
        workers=args.workers,
        chunk_size=args.chunk_size,
        fdb_retries=args.fdb_retries,
        fdb_retry_initial_s=args.fdb_retry_initial_s,
        fdb_retry_max_s=args.fdb_retry_max_s,
        lock_timeout_s=args.lock_timeout_s,
        through_step=args.through_step,
    )
    print(json.dumps(state, indent=2, sort_keys=True))
    return 0 if state.get("status") != "critical" else 1


if __name__ == "__main__":
    raise SystemExit(main())
