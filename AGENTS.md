# AGENTS.md

## Purpose

`precip_type_diag` is the MeteoSwiss FDB-only precipitation-type diagnostic for
`ICON-CH1-EPS`, `ICON-CH2-EPS`, and deterministic `ICON-REA-L-CH1`. It runs on
Balfrin and offers two modes:

- `firdewsa` (default): thesis-faithful Modified Bourgouin implementation.
- `icon`: offline adaptation pinned to ICON commit
  `50da7c5924994f7626688eb5185b8e66c781b12e`.

Member output is categorical `PTYPE` in GRIB2 (default) or NetCDF. Ensemble
probabilities require NetCDF.

## Before Editing

1. Read `README.md`; read `docs/science-and-architecture.md` for science, FDB,
   format, or orchestration changes.
2. Check `git status` and preserve unrelated work.
3. For code discovery, use the knowledge graph in this order: `search_graph`,
   `trace_path`, `get_code_snippet`. Use text search for literals, scripts,
   configuration, and documentation.
4. Choose focused tests before changing code.

Operational guidance is in `docs/release-and-operations.md`; release evidence
belongs in `docs/release-checklist.md`; source and licensing notes are in
`docs/provenance.md`.

## Authoritative Code

- `profile.py` / `numba_backend.py`: Firdewsa reference and accelerated path.
- `icon_profile.py` / `icon_numba_backend.py`: ICON scalar reference and
  accelerated path.
- `grid.py`: array validation and grid diagnosis.
- `operational.py`: FDB contracts, retrieval, processing, output, and summaries.
- `gribio.py`, `netcdfio.py`, `probabilities.py`: product formats and aggregation.
- `monitoring.py`: operational status and exit contract.
- `definitions/`: packaged ecCodes `PTYPE` overlay.
- `tools/run_depl_cycle.sh`: realtime fixed-cycle wrapper; it does not submit jobs.

## Contracts to Preserve

- Scientific constants and behavior change only for a demonstrated bug or an
  explicit scientific decision. Optimized paths must match their scalar
  references; ICON science changes also require the executable Fortran harness.
- Preserve category codes `0, 1, 3, 5, 6, 7, 8, 9, 10, 12, 13`, GRIB metadata,
  and the `summary.json` contract unless the product contract changes explicitly.
- Required fields are `T`, `P`, `QV`, `HHL`, `TOT_PREC`, and `T_G`; ICON mode also
  needs `RAIN_GSP`, `SNOW_GSP`, and `GRAU_GSP`. Missing convective and hail rates
  remain explicit fidelity limitations.
- Hourly amounts are adjacent accumulation differences. Realtime accumulations
  start at the forecast cycle; REA-L-CH1 accumulates from its daily `0000` cycle
  through step 24 and must never cross a day boundary.
- CH1: members `000..010`, step 33. CH2: `000..020`, step 120. REA-L-CH1:
  member `000`, step 24, explicit date and `time=0000`.
- Default processing uses step 1 onward, 8 member workers, 2-hour chunks,
  prefetch, and GRIB2. Probability aggregation is strict across requested
  members and uses percent values (`0..100`).
- Fail visibly on deterministic science, shape, validation, and completeness
  errors. Retry only transient FDB list, retrieve, and decode failures.
- Runs publish `RUNNING.json`, then atomically `DONE.json` or `FAILED.json`;
  critical monitoring alerts must return a non-zero CLI exit.
- Do not change dependencies, ecCodes definitions, the 12 km cutoff, masks, or
  probability thresholds without production justification and appropriate
  scientific/operational review.

Real FDB tests are manual on Balfrin; CI stays synthetic and mocked. There is no
file-input production path, plotting, bias correction, or station processing.

## Validation

Use focused tests while iterating. Before handoff, commit, or push, run:

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
git diff --check
```

Also build and inspect a wheel after packaging changes. Run the README Balfrin
smokes after production-facing changes; dual-mode releases cover all three
models. Use `pp-short` for manual jobs that fit its limit.

## Change Hygiene

Keep changes narrow, add regression tests for bugs, update operator docs when a
public or operational contract changes, and inspect the complete diff before
committing. Never commit generated outputs, environments, caches, logs, secrets,
SSH material, or machine-specific paths.
