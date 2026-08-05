# AGENTS.md

## Purpose

`precip_type_diag` is the MeteoSwiss FDB-only precipitation-type diagnostic for
`ICON-CH1-EPS`, `ICON-CH2-EPS`, and deterministic `ICON-REA-L-CH1`. It runs on
Balfrin and offers two modes:

- `firdewsa` (default): thesis-faithful Modified Bourgouin implementation.
- `icon`: offline adaptation pinned to ICON commit
  `50da7c5924994f7626688eb5185b8e66c781b12e`.

The accepted wrappers use NetCDF plus strict ensemble probabilities for
realtime EPS and atomically published monthly, multi-message categorical GRIB2
archives for REA backfills. The lower-level CLI also supports either member
format.

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
- `realtime.py`: progressive EPS discovery, cycle state, and incremental publish.
- `backfill.py`: REA inventory manifests, monthly tasks, bounded daily staging,
  atomic archives, receipts, and status.
- `gribio.py`, `netcdfio.py`, `probabilities.py`: product formats and aggregation.
- `monitoring.py`: operational status and exit contract.
- `definitions/`: packaged ecCodes `PTYPE` overlay.
- `tools/setup_balfrin.sh`: reviewed one-command Balfrin runtime setup.
- `tools/run_balfrin.sh`: unified accepted operator entry point.
- `tools/run_depl_cycle.sh`: progressive explicit-cycle compatibility wrapper;
  it does not submit jobs.

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
  through step 24 and must never cross a day boundary. A negative within-cycle
  difference is nonphysical: clamp it to zero and count it in data quality for
  `TOT_PREC` and ICON's archived microphysics; never substitute the current
  accumulator as a reset amount. Small decreases can be GRIB packing noise;
  unexpected counts require upstream review.
- CH1: members `000..010`, maximum step 45; current `0300` cycles use 45
  and other cycles use 33. CH2: `000..020`, step 120. REA-L-CH1:
  member `000`, step 24, explicit date and `time=0000`.
- Generic processing uses step 1 onward, 8 member workers, 2-hour chunks,
  prefetch, and GRIB2. Accepted realtime processing uses all members, NetCDF,
  and probabilities; accepted REA processing uses member `000`, steps `1..24`,
  and GRIB2. Probability aggregation is strict and uses percent (`0..100`).
- Realtime publication advances only through contiguous complete hours and
  preserves earlier products. REA manifests group independent daily cycles into
  monthly tasks; cycle `D` step 24 is valid at `D+1 00 UTC` but remains part of
  cycle `D`.
- REA backfill schema v2 stages each daily cycle outside the archive root,
  concatenates its 24 verified GRIB2 messages in cycle-date/step order, and
  atomically publishes one file per month. One task owns a month; never allow
  concurrent append. Schema-v1 daily manifests remain tied to `v0.3.0`.
- Preserve immutable `CONTRACT.json`, progressive `CYCLE.json`, locks, verified
  resume, per-increment evidence, `ARCHIVE_CONTRACT.json`, and monthly campaign
  receipt/status contracts.
- Fail visibly on deterministic science, shape, validation, and completeness
  errors. Retry only transient FDB list, retrieve, and decode failures.
- Runs publish `RUNNING.json`, then atomically `DONE.json` or `FAILED.json`;
  critical monitoring alerts must return a non-zero CLI exit.
- Do not change dependencies, ecCodes definitions, the 12 km cutoff, masks, or
  probability thresholds without production justification and appropriate
  scientific/operational review.

Real FDB tests are scheduled on Balfrin; CI stays synthetic and mocked. There is no
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

Also build and inspect a wheel after packaging changes. Run the release
checklist's Balfrin acceptance matrix after production-facing changes,
including a real multi-message monthly GRIB smoke and restart;
dual-mode FDB/science releases cover all three models. Use `pp-short` for jobs
that fit its limit and `pp-long` for generated REA arrays.

## Agent instruction consistency

`AGENTS.md` is the repository's agent-facing source of truth; this repository
currently contains no `SKILL.md` packages. If a repository-local skill is added,
keep its Balfrin image, views, model horizons, accepted wrappers, REA date
semantics, and validation gate consistent with this file and the README.

## Change Hygiene

Keep changes narrow, add regression tests for bugs, update operator docs when a
public or operational contract changes, and inspect the complete diff before
committing. Never commit generated outputs, environments, caches, logs, secrets,
SSH material, or machine-specific paths.
