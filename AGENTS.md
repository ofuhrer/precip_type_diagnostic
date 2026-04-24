# AGENTS.md

## Purpose

`precip_type_diag` implements a categorical precipitation-type diagnostic for
MeteoSwiss `ICON-CH1-EPS` and `ICON-CH2-EPS`. It follows the Zukanovic
MSc-thesis method based on the Modified Bourgouin algorithm.

Outputs are one categorical GRIB2 `PTYPE` field per member/forecast hour and, in
operational mode, a `summary.json`. The scope is intentionally limited: no
ensemble aggregation, probability GRIBs, bias correction, or alternative
diagnostics.

## Layout

- `src/precip_type_diag/profile.py`: pure Python column reference logic.
- `src/precip_type_diag/numba_backend.py`: accelerated categorical backend.
- `src/precip_type_diag/grid.py`: grid preparation and production diagnosis.
- `src/precip_type_diag/gribio.py`: ecCodes setup, GRIB reading/writing, job discovery.
- `src/precip_type_diag/operational.py`: full-run member processing and summaries.
- `src/precip_type_diag/definitions/`: local ecCodes overlay for `PTYPE`; edit carefully.
- `test/`: pytest suite with synthetic, mocked, and optional real-GRIB tests.
- `docs/`: discovery and acceleration notes.

Large GRIB fixtures under `test/fixtures/` are local-only and ignored by git.

## Commands

```bash
python -m pip install -e ".[test]"
python -m py_compile src/precip_type_diag/*.py test/*.py
PYTHONPATH=src python -m pytest -q
```

Run examples:

```bash
PYTHONPATH=src python -m precip_type_diag --input-run /path/to/run/icon --model ICON-CH2-EPS --report-only
PYTHONPATH=src python -m precip_type_diag --input-root /opr/osm/inn/cache --output-root /path/to/output --model ICON-CH2-EPS --run latest
PYTHONPATH=src python -m precip_type_diag.benchmark --case real-ch2
```

No formatter, linter, type checker, or CI workflow is currently configured.

## Constraints

- Keep changes conservative and thesis-faithful unless fixing a clear bug.
- Preserve public CLI flags, output codes, GRIB metadata, and `summary.json`
  shape unless there is a compelling reason.
- The reference algorithm lives in `profile.py`; production optimizations must
  match it for tested cases.
- Do not change scientific constants, `PTYPE` definitions, or
  `src/precip_type_diag/definitions/` casually.
- Debug/single-job mode should fail loudly. Operational mode may record member
  or step failures in summaries.
- Avoid broad silent fallbacks. If fallback is necessary, keep it explicit and
  covered by tests.
- Do not commit generated caches, fetched fixtures, operational outputs, secrets,
  SSH material, or machine-specific paths.

## Domain Notes

- Required fields: `T`, `P`, `QV`, `HHL`, `TOT_PREC`, `T_G`.
- Forecast-step strings use ICON `DDHHMMSS`.
- Hourly precipitation is current `TOT_PREC` minus previous-hour `TOT_PREC`.
- Production uses ecCodes I/O, `numba`, and a fixed 12 km vertical cutoff from
  `HHL`.
- Output category codes are `0, 1, 3, 5, 8, 12, 13`.

## Test Guidance

- Add or update tests for behavior-changing fixes.
- Prefer synthetic tests for algorithm changes and mocked tests for orchestration
  or I/O behavior.
- Use real fixtures only for behavior that depends on real GRIB encoding or
  metadata.
- Real-fixture tests may skip when local ecCodes/MeteoSwiss definitions cannot
  decode the files.
