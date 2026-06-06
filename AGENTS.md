# AGENTS.md

## Purpose

`precip_type_diag` implements a categorical precipitation-type diagnostic for
MeteoSwiss `ICON-CH1-EPS` and `ICON-CH2-EPS`. It follows Firdewsa Zukanovic's
MSc thesis method, based on the Modified Bourgouin algorithm.

The production path is FDB-only. It reads realtime FDB fields on Balfrin and
writes one categorical GRIB2 `PTYPE` field per member/forecast hour plus
`summary.json` and `monitoring.json`. When `--write-probability-products` is
enabled, it also writes member diagnostic NetCDF sidecars and final ensemble
probability NetCDF products.

## Read First

- `README.md`: fresh setup and operator run instructions.
- `docs/science-and-architecture.md`: scientific method, input/output contract,
  and FDB architecture.

## Layout

- `src/precip_type_diag/profile.py`: pure Python column reference logic.
- `src/precip_type_diag/numba_backend.py`: accelerated categorical and
  microphysics-probability backend.
- `src/precip_type_diag/grid.py`: grid preparation and production diagnosis.
- `src/precip_type_diag/gribio.py`: ecCodes definition setup, vertical
  truncation, and GRIB writing.
- `src/precip_type_diag/netcdfio.py`: NetCDF sidecar/product read and write helpers.
- `src/precip_type_diag/probabilities.py`: member sidecar schema and strict
  ensemble probability aggregation.
- `src/precip_type_diag/monitoring.py`: machine-readable operational status and
  alert evaluation from `summary.json`.
- `src/precip_type_diag/operational.py`: FDB discovery, completeness checks, retrieval,
  prefetching, member multiprocessing, and summaries.
- `src/precip_type_diag/definitions/`: local ecCodes overlay for `PTYPE`.
- `test/`: pytest suite with synthetic and mocked orchestration tests.

There is no file-based input path, no fixture-fetch script, and no bundled real
GRIB fixture data.

## Commands

```bash
python -m pip install -e ".[test,dev]"
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
```

Run a Balfrin smoke test:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

Run a full CH2 production-style job:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members all \
  --workers 4 \
  --output-root /users/$USER/work/ptype-fdb
```

Ruff, mypy, pytest coverage, and GitHub Actions checks are configured.

## Constraints

- Keep changes thesis-faithful unless fixing a clear bug.
- Preserve output category codes, GRIB metadata, and `summary.json` shape unless
  there is a compelling reason.
- The reference algorithm lives in `profile.py`; production optimizations must
  match it for tested cases.
- Do not change scientific constants, `PTYPE` definitions, or
  `src/precip_type_diag/definitions/` casually.
- Avoid broad silent fallbacks. Operational member failures may be recorded in
  summaries, but debug failures should stay visible.
- Do not add dependencies unless there is a clear production need.
- Do not commit generated caches, operational outputs, secrets, SSH material, or
  machine-specific paths.

## Domain Notes

- Required fields: `T`, `P`, `QV`, `HHL`, `TOT_PREC`, `T_G`.
- Hourly precipitation is current `TOT_PREC` minus previous-hour `TOT_PREC`.
- Production starts at forecast step 1 by default; step 0 is used only as the
  previous `TOT_PREC` field for the first hourly delta.
- Production uses FDB, ecCodes I/O, NetCDF I/O, `numba`, and a fixed 12 km
  vertical cutoff from `HHL`.
- Prefetching is enabled by default; `--no-prefetch` is for debugging or timing
  comparison.
- Runs write both `summary.json` and `monitoring.json`; critical monitoring
  alerts make the CLI exit non-zero.
- Optional probability products use percent scale `0..100`, a 30% probability
  threshold, and a 0.01 mm/h precipitation mask for thresholded intensity fields.
- Output category codes are `0, 1, 3, 5, 8, 12, 13`.
