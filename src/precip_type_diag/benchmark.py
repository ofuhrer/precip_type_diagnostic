"""Benchmark entrypoint for the categorical precipitation-type diagnostic."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import time

import numpy as np

from .gribio import MemberHourJob, load_member_hour
from .grid import GridInputs, diagnose_grid_categorical


DEFAULT_REAL_FIXTURE_DIR = Path("test/fixtures/real_icon_ch2_eps")
DEFAULT_REAL_STEP = "04180000"
DEFAULT_REAL_PREVIOUS_STEP = "04170000"


@dataclass(frozen=True)
class BenchmarkResult:
    case: str
    load_s: float
    diagnose_s: float
    active_columns: int
    total_columns: int
    retained_full_levels: int | None = None

    @property
    def columns_per_second(self) -> float:
        if self.diagnose_s <= 0.0:
            return 0.0
        return self.active_columns / self.diagnose_s


def _real_fixture_job(fixture_dir: Path) -> MemberHourJob:
    return MemberHourJob(
        member="000",
        step=DEFAULT_REAL_STEP,
        current_file=fixture_dir / f"lfff{DEFAULT_REAL_STEP}",
        previous_file=fixture_dir / f"lfff{DEFAULT_REAL_PREVIOUS_STEP}",
        constants_file=fixture_dir / "lfff00000000c",
    )


def _synthetic_inputs(n_levels: int = 80, n_columns: int = 20000) -> GridInputs:
    heights = np.linspace(10000.0, 0.0, n_levels + 1, dtype=float)
    half_level_height_m = np.repeat(heights[:, np.newaxis], n_columns, axis=1)

    temperature_top = np.linspace(253.15, 279.15, n_levels, dtype=float)
    temperature_k = np.repeat(temperature_top[:, np.newaxis], n_columns, axis=1)
    pressure_pa = np.repeat(np.linspace(30000.0, 100000.0, n_levels, dtype=float)[:, np.newaxis], n_columns, axis=1)
    specific_humidity = np.repeat(np.linspace(0.0004, 0.0040, n_levels, dtype=float)[:, np.newaxis], n_columns, axis=1)

    warm_nose_start = n_levels // 2
    warm_nose_stop = min(warm_nose_start + 8, n_levels)
    temperature_k[warm_nose_start:warm_nose_stop, ::2] += 8.0
    temperature_k[-8:, 1::4] -= 4.0

    total_precip_mm = np.zeros(n_columns, dtype=float)
    total_precip_mm[: n_columns // 2] = 1.5
    total_precip_mm[n_columns // 2 : (3 * n_columns) // 4] = 0.2
    ground_temperature_c = np.where(np.arange(n_columns) % 3 == 0, -4.0, 2.0)

    return GridInputs(
        temperature_k=temperature_k,
        pressure_pa=pressure_pa,
        specific_humidity=specific_humidity,
        half_level_height_m=half_level_height_m,
        total_precip_mm=total_precip_mm,
        ground_temperature_c=ground_temperature_c,
    )


def _measure_diagnostic(
    inputs: GridInputs,
    *,
    chunk_size: int,
    repeat: int,
) -> tuple[float, np.ndarray]:
    diagnose_grid_categorical(inputs, chunk_size=chunk_size)

    best_time = float("inf")
    latest = np.array([], dtype=np.int32)
    for _ in range(repeat):
        start = time.perf_counter()
        latest = diagnose_grid_categorical(inputs, chunk_size=chunk_size)
        elapsed = time.perf_counter() - start
        if elapsed < best_time:
            best_time = elapsed
    return best_time, latest


def run_real_case(
    *,
    fixture_dir: Path,
    chunk_size: int,
    repeat: int,
) -> BenchmarkResult:
    start = time.perf_counter()
    inputs, _, selection = load_member_hour(_real_fixture_job(fixture_dir))
    load_s = time.perf_counter() - start

    diagnose_s, _ = _measure_diagnostic(inputs, chunk_size=chunk_size, repeat=repeat)
    active_columns = int(np.count_nonzero(inputs.total_precip_mm > 0.0))
    return BenchmarkResult(
        case="real_ch2_fixture",
        load_s=load_s,
        diagnose_s=diagnose_s,
        active_columns=active_columns,
        total_columns=int(np.asarray(inputs.total_precip_mm).size),
        retained_full_levels=selection.retained_full_levels,
    )


def run_synthetic_case(
    *,
    chunk_size: int,
    repeat: int,
    n_levels: int,
    n_columns: int,
) -> BenchmarkResult:
    inputs = _synthetic_inputs(n_levels=n_levels, n_columns=n_columns)
    diagnose_s, _ = _measure_diagnostic(inputs, chunk_size=chunk_size, repeat=repeat)
    active_columns = int(np.count_nonzero(inputs.total_precip_mm > 0.0))
    return BenchmarkResult(
        case="synthetic_active_columns",
        load_s=0.0,
        diagnose_s=diagnose_s,
        active_columns=active_columns,
        total_columns=int(np.asarray(inputs.total_precip_mm).size),
        retained_full_levels=n_levels,
    )


def _print_result(result: BenchmarkResult) -> None:
    print(f"case={result.case}")
    print("backend=numba")
    print("reader_backend=fast")
    print(f"load_s={result.load_s:.3f}")
    print(f"diagnose_s={result.diagnose_s:.3f}")
    print(f"active_columns={result.active_columns}")
    print(f"total_columns={result.total_columns}")
    print(f"retained_full_levels={result.retained_full_levels}")
    print(f"columns_per_second={result.columns_per_second:.1f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("real-ch2", "synthetic"), default="real-ch2")
    parser.add_argument("--chunk-size", type=int, default=4096)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_REAL_FIXTURE_DIR)
    parser.add_argument("--n-levels", type=int, default=80)
    parser.add_argument("--n-columns", type=int, default=20000)
    args = parser.parse_args(argv)

    if args.case == "real-ch2":
        result = run_real_case(
            fixture_dir=args.fixture_dir,
            chunk_size=args.chunk_size,
            repeat=args.repeat,
        )
    else:
        result = run_synthetic_case(
            chunk_size=args.chunk_size,
            repeat=args.repeat,
            n_levels=args.n_levels,
            n_columns=args.n_columns,
        )
    _print_result(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
