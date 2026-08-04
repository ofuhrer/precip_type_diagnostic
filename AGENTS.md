# AGENTS.md

## Mission

`precip_type_diag` is the MeteoSwiss categorical precipitation-type diagnostic
for `ICON-CH1-EPS` and `ICON-CH2-EPS`. It implements Firdewsa Zukanovic's MSc
thesis method, based on the Modified Bourgouin algorithm.

Production is FDB-only and runs on Balfrin. It writes one categorical `PTYPE`
field per requested member and forecast hour, operational JSON, and a run-state
marker. GRIB2 is the default member format. NetCDF is optional and required for
ensemble probability products.

## Start of Every Task

1. Read `README.md` for setup and operator workflows.
2. Read `docs/science-and-architecture.md` before changing science, data
   contracts, formats, or orchestration.
3. Check `git status` and preserve unrelated worktree changes.
4. Use the codebase knowledge graph for code discovery: `search_graph`, then
   `trace_path`, then `get_code_snippet`. Use text search for literals,
   configuration, shell scripts, and documentation.
5. Identify the smallest relevant validation tier before editing.

Additional references:

- `docs/release-and-operations.md`: promotion, monitoring, and rollback.
- `docs/release-checklist.md`: release evidence template.
- `docs/provenance.md`: sources, licensing, and bundled PDFs.

## Source Map

- `profile.py`: authoritative pure-Python column algorithm.
- `numba_backend.py`: accelerated categorical and microphysics-probability
  implementation; must remain behaviorally aligned with `profile.py`.
- `grid.py`: array preparation, active-column selection, and grid diagnosis.
- `operational.py`: FDB discovery/checks/retrieval, retries, prefetching,
  multiprocessing, run markers, and summaries.
- `gribio.py`: ecCodes definitions, vertical truncation, and GRIB2 writing.
- `netcdfio.py`: atomic generic NetCDF read/write helpers.
- `probabilities.py`: member NetCDF schema and strict ensemble aggregation.
- `monitoring.py`: operational status and alert evaluation.
- `provenance.py`: runtime, dependency, and Git provenance.
- `definitions/`: packaged local ecCodes overlay for `PTYPE`.
- `tools/run_depl_cycle.sh`: fixed-cycle DEPL wrapper; it does not submit jobs.
- `test/`: synthetic science tests and mocked orchestration tests. No real GRIB
  fixture data or fixture-fetch path exists.

## Non-Negotiable Contracts

- Keep scientific behavior thesis-faithful unless correcting a demonstrated
  bug. Do not tune constants or thresholds opportunistically.
- Preserve category codes `0, 1, 3, 5, 8, 12, 13`, GRIB metadata, and the
  `summary.json` contract unless an explicit product decision changes them.
- Treat `profile.py` as the scientific reference. Any optimized path change
  requires focused reference-parity tests.
- Do not casually change `definitions/`, the 12 km `HHL` cutoff, probability
  thresholds, or precipitation masks; these require scientific and operational
  review.
- Do not add broad silent fallbacks. Infrastructure failures may be retried or
  recorded per member; deterministic science, shape, validation, and strict
  completeness failures must remain visible.
- Do not add dependencies without a production justification and a Balfrin
  compatibility check.

## Operational Contract

- Required FDB fields: `T`, `P`, `QV`, `HHL`, `TOT_PREC`, `T_G`.
- Hourly precipitation is `TOT_PREC(current) - TOT_PREC(previous)`.
- Default production starts at step 1; step 0 supplies only the first previous
  accumulated-precipitation field.
- CH1 has members `000..010` and max step 33. CH2 has members `000..020` and max
  step 120.
- Defaults: 8 member workers, 2-hour chunks, prefetch enabled, GRIB2 output.
- Probability aggregation is strict across every requested member. Values use
  percent scale `0..100`; thresholded intensity uses 30% probability and a
  `0.01 mm/h` precipitation mask.
- Critical monitoring alerts produce a non-zero CLI exit. Runs publish
  `RUNNING.json`, then `DONE.json` or `FAILED.json` atomically.
- Retries cover transient FDB list, retrieve, and decode/materialization
  failures only.
- Manual Balfrin development jobs should use the generally open `pp-short`
  partition when they fit its limit. Do not default to restricted production
  partitions.

## Known Boundaries

- There is no file-based production input path, plotting layer, bias correction,
  or station postprocessing.
- NetCDF currently records generic `cell` or `y/x` dimensions without
  geospatial coordinate variables or a grid mapping. Adding those is a product
  contract change, not a cosmetic refactor.
- Background PDFs are review material, not runtime package data. Preserve their
  provenance and do not assume redistribution rights.
- Real FDB verification is manual on Balfrin; CI must remain synthetic/mocked.

## Validation

Focused tests are acceptable during iteration. Before handoff, commit, or push,
run the complete local gate:

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
git diff --check
```

For changes to packaging, also build a wheel and confirm the ecCodes definition
files are present. For production-facing changes, run the Balfrin smoke commands
from `README.md`; formal releases test both models from the candidate tag.

## Change Hygiene

- Keep generated caches, virtual environments, build artifacts, operational
  outputs, logs, secrets, SSH material, and machine-specific paths out of Git.
- Make the narrowest coherent change; avoid mixing scientific changes with
  mechanical refactors.
- Add regression tests for every fixed bug and verify public CLI errors at both
  programmatic and command-line boundaries where relevant.
- Update operator docs when defaults, environment requirements, output layouts,
  monitoring behavior, or wrapper arguments change.
- Before committing, inspect the complete diff, verify package-data changes,
  and split unrelated concerns into focused commits.
