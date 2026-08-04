# Science and Architecture

This document explains why the diagnostic works as it does and how the
production components fit together. For installation and first-run commands,
start with the [README](../README.md).

The shortest useful mental model is: retrieve one atmospheric column from FDB,
derive its wet-bulb thermal structure, classify its surface precipitation type,
and repeat that process for every grid point, member, and forecast hour.

## Scope

`precip_type_diag` produces one categorical precipitation-type field per ICON
member and forecast hour for:

- `ICON-CH1-EPS`
- `ICON-CH2-EPS`
- `ICON-REA-L-CH1` (deterministic member `000`)

By default the package writes categorical member `PTYPE` GRIB2 outputs. With
`--output-format=netcdf`, it writes member `PTYPE` NetCDF outputs instead. With
`--output-format=netcdf --write-probability-products`, those member NetCDF files
also contain diagnostic fields and the run writes ensemble probability NetCDF
products. Aggregation is strict: every member requested for the run must finish
successfully. The package does not produce plotting, bias correction, station
postprocessing, or alternative diagnostics.

## Scientific Modes

The default `firdewsa` mode follows Firdewsa Zukanovic's MSc thesis,
*Precipitation Type Diagnostic for ICON*. It remains isolated in `profile.py`
and `numba_backend.py`; selecting or adding the ICON mode does not alter its
formulas or default behavior.

The optional `icon` mode follows the diagnostic introduced into ICON by commit
[`50da7c5924994f7626688eb5185b8e66c781b12e`](https://gitlab.dkrz.de/icon/icon-nwp/-/commit/50da7c5924994f7626688eb5185b8e66c781b12e).
Its scalar reference is `icon_profile.py`, with a behaviorally matched accelerated
path in `icon_numba_backend.py`. Relative to the thesis path, it uses ICON's
native vapor-pressure and ice-saturation formulas, interface-height layer
depths, zero-crossing energy integration, sign-based area interpretation, the
corrected all-column melting energy, the `0.29` snow-energy coefficient,
surface ice-pellet melting thresholds, large-refreezing ice-pellet suppression,
an interval-scaled trace mask, negative-accumulation reset handling, and
surface-microphysics refinements.

The broader method lineage is:

- Bourgouin (2000): original area method using melting and refreezing energy
  from the vertical thermal profile.
- Birk et al. (2021): revised/Modified Bourgouin method, including wet-bulb
  profile usage and ice-nucleation considerations.
- Zukanovic MSc thesis: ICON/MeteoSwiss implementation choices mirrored by this
  repository. See the
  [MeteoSwiss thesis prototype](https://github.com/MeteoSwiss-APN/precip_diagnostic)
  for code.

## FDB Sources

The model name selects both the model metadata and the expected FDB view:

| CLI model | uenv view | FDB identity | Cycle contract |
| --- | --- | --- | --- |
| `ICON-CH1-EPS` | `realtime` | `od/enfo/0001/icon-ch1-eps` | Rolling forecast cycles; latest complete cycle can be discovered |
| `ICON-CH2-EPS` | `realtime` | `od/enfo/0001/icon-ch2-eps` | Rolling forecast cycles; latest complete cycle can be discovered |
| `ICON-REA-L-CH1` | `rea-l-ch1` | `rd/reanl/r001/icon-rea-l-ch1` | One deterministic `0000` cycle per day, steps `0..24`; date and time must be explicit |

The REA-L archive spans 2005–2025. Its FDB schema mixes 10-minute and hourly
steps for some surface variables, so completeness queries always constrain the
hourly step range. The archive does not expose `timespan` in `fdb-utils` list
metadata; retrieval still requests `timespan=none` for instantaneous fields and
`timespan=fs` for accumulated fields, while completeness is checked from the
explicit step and level inventory.

The realtime FDB inventory contains ICON-CH1-EPS and ICON-CH2-EPS. KENDA-CH1
shares operational model workflows but is not documented or observed as a
model in the realtime FDB view and is therefore not exposed by this tool.

## Input Fields

All supported FDB sources fetch these core fields:

| Field | MeteoSwiss `paramId` | Role |
| --- | ---: | --- |
| `T` | `500014` | full-level temperature |
| `P` | `500001` | full-level pressure |
| `QV` | `500035` | full-level specific humidity |
| `HHL` | `500008` | half-level height, fetched at step 0 |
| `TOT_PREC` | `500041` | accumulated precipitation |
| `T_G` | `500010` | ground temperature |

Hourly precipitation is diagnosed as:

```text
TOT_PREC(current step) - TOT_PREC(previous step)
```

By default production starts at step 1 because step 0 has no preceding hourly
forecast interval. `TOT_PREC(step 0)` is fetched only to initialize the first
hourly delta:

```text
TOT_PREC(step 1) - TOT_PREC(step 0)
```

The CLI still accepts `--start-step 0` for debugging or compatibility, but that
output should be treated as a step-0 placeholder rather than a physically
well-defined hourly precipitation-type diagnostic.

For the realtime forecasts, accumulated fields start at each model forecast
cycle. For REA-L-CH1, `TOT_PREC` and the grid-scale microphysics fields are
accumulated from the daily 00 UTC cycle through step 24. The same
current-minus-previous operation therefore produces hourly amounts in both
sources, provided REA-L processing stays within a single explicitly selected
day. The implementation enforces `time=0000` and a maximum step of 24 for
REA-L-CH1 and never carries an accumulator across daily cycles.

ICON mode additionally requires the accumulated grid-scale microphysics fields
available in both reviewed FDB inventories:

| Field | MeteoSwiss `paramId` | Offline use |
| --- | ---: | --- |
| `RAIN_GSP` | `500134` | hourly-mean grid-scale rain rate |
| `SNOW_GSP` | `500053` | hourly-mean grid-scale snow rate |
| `GRAU_GSP` | `500146` | hourly-mean grid-scale graupel rate |

Each rate is derived from the current-minus-previous accumulation and divided
by 3600 seconds. A negative accumulation delta is treated as a reset and uses
the current accumulation, matching the ICON total-precipitation handling.
Completeness checks require all three fields in ICON mode.

This is intentionally reported as partial online fidelity. The online ICON
call also receives convective rain, convective snow, and hail rates. Those
components are not present in the reviewed CH1/CH2 FDB inventory, so the
offline run cannot reproduce refinements that depend on them. They are not
silently inferred: `summary.json["algorithm_fidelity"]` names the available and
unavailable components and sets exact online refinement parity to false.

## Column Algorithms

The original pure Python reference implementation is in `profile.py`.

For each active column:

1. Convert temperature to Celsius.
2. Derive dew point, wet-bulb temperature, and relative humidity over ice.
3. Identify precipitating and sublimating layers.
4. Estimate ice probability from the precipitation-generation layer.
5. Compute melting and refreezing energies from the wet-bulb profile.
6. Convert the resulting probabilities to one categorical code by selecting the
   highest-probability type, using the fixed priority order in `constants.py`
   only for ties.

The production grid path in `grid.py` dispatches the selected mode through its numba
backend for speed. Dry columns, defined by
`total_precip_mm <= precip_mask_threshold_mm`, are assigned `NO_PRECIP` directly.
This means negative hourly precipitation deltas are treated as non-active
precipitation under the current threshold logic.

Input data quality is checked before active columns are diagnosed. Non-finite
hourly precipitation is fatal because the activity mask cannot be trusted.
Non-finite temperature, pressure, humidity, height, or ground temperature in an
active precipitation column is also fatal. Non-finite profile values in dry
columns are counted in the data-quality summary but do not affect the categorical
output because dry columns are assigned `NO_PRECIP` without thermodynamic
diagnosis.

ICON mode additionally requires positive temperature and pressure, strictly
descending half-level interfaces, and finite supplied surface microphysics
rates in every active column. Its trace comparison follows ICON exactly:
precipitation amounts equal to the threshold are classified as no precipitation.

## Output Codes

The categorical output is encoded as MeteoSwiss `PTYPE`:

| Code | Meaning |
| ---: | --- |
| `0` | no precipitation |
| `1` | rain |
| `3` | freezing rain |
| `5` | snow |
| `6` | wet snow |
| `7` | mixture of rain and snow |
| `8` | ice pellets |
| `9` | graupel |
| `10` | hail |
| `12` | freezing drizzle |
| `13` | freezing rain on ground |

The package includes a small ecCodes overlay for the local `PTYPE` metadata and
the additional code-table entry.

## Operational Architecture

The production CLI is FDB-only:

```text
FDB discovery and completeness checks
  -> HHL retrieval and vertical-level selection
  -> hourly field chunk retrieval
  -> decode arrays
  -> diagnose categorical PTYPE
  -> write one member output file per member/step
  -> optionally aggregate NetCDF probability products across all requested members
  -> write summary.json, monitoring.json, and run-state marker
```

Important implementation details:

- `operational.py` owns FDB discovery, completeness checks, retrieval, chunk
  prefetching, member-level multiprocessing, and summaries.
- Operational runs emit Python logging records for run start, discovery,
  transient FDB retries, per-step processing, member failures, probability
  generation, and completion. The CLI can emit text or JSON logs.
- Each worker process handles one member at a time.
- Within a member, chunk prefetching overlaps the next FDB request with decoding,
  diagnosis, and writing of the current chunk.
- `gribio.py` owns ecCodes definition setup, vertical truncation, and GRIB output
  writing.
- `write_output_grib()` uses the current-hour `TOT_PREC` FDB field as the output
  template, preserving grid geometry and run/member/step metadata while replacing
  parameter metadata and values. It checks output shape, finite integer
  category values, and the allowed `PTYPE` code set before writing.
- `netcdfio.py` owns NetCDF member and probability-product writing. Member
  NetCDF outputs always contain `PTYPE`; when probability products are enabled,
  they also contain hourly precipitation and Firdewsa-style
  microphysics-consistent per-type probabilities in percent. The probability
  module then writes one final NetCDF per step under `probabilities/` with
  ensemble probability means, thresholded precipitation overlays, categorical
  `PTYPE` frequencies, valid member count, and mean hourly precipitation.
  Firdewsa output retains its original categorical-frequency schema; ICON mode
  additionally emits frequencies for codes `6`, `7`, `9`, and `10`.
- `summary.json` includes runtime provenance: Python/platform metadata,
  dependency versions, Git commit, branch, dirty-worktree state, and command-line
  arguments when available.
- `summary.json` also carries production run metadata, retry policy and retry
  counters, performance counters for completeness checks, static-field
  retrieval, dynamic FDB request groups, decode groups, diagnosis, writes,
  forecast-hour chunk counts, and probability-stage NetCDF read, aggregation,
  write, and publish phases.
- Each run writes `RUNNING.json` at start and replaces it with `DONE.json` or
  `FAILED.json` according to the final monitoring status.
- FDB retries are limited to transient infrastructure operations: `fdb-utils`
  list calls, FDB field retrieval, and field materialization/decode. Incomplete
  FDB contents, invalid data, and strict probability completeness failures are
  surfaced directly instead of silently retried.

## Operational Defaults

| Setting | Default |
| --- | --- |
| CH1 members | `000..010` |
| CH2 members | `000..020` |
| REA-L-CH1 members | `000` |
| CH1 max step | `33` |
| CH2 max step | `120` |
| REA-L-CH1 max step | `24` |
| start step | `1` |
| worker count | `8` unless overridden |
| chunk size | `2` forecast hours |
| prefetch | enabled |
| output format | `grib2` |
| scientific algorithm | `firdewsa` |
| vertical cutoff | `12000 m` |
| precipitation mask threshold, Firdewsa | `0.0 mm` |
| precipitation mask threshold, ICON | `0.01 mm` for the hourly interval |

The vertical cutoff is derived from `HHL`; levels above the cutoff are discarded
before diagnosis. The cutoff is a performance optimization and should not be
changed without scientific review.

## Test Strategy

The test suite covers four areas:

- `test_profile.py` and `test_numba_backend.py`: original Firdewsa science and
  accelerated-reference parity checks.
- `test_icon_profile.py`: frozen vectors produced by executing the exact ICON
  Fortran source at the pinned commit.
- `test_grid.py`: grid data-quality behavior and ICON scalar/numba/Fortran-vector parity.
- `test_probabilities.py`: NetCDF member output, aggregation, and strict completeness
  checks.
- `test_operational.py` and `test_cli.py`: mocked FDB orchestration and CLI
  behavior.

Real FDB access is checked manually on Balfrin with a smoke run, for example:

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

Formal releases should rerun the smoke test from the annotated release tag.
Run it once with the default Firdewsa mode and once with `--algorithm icon` for
each model when promoting changes to the dual-mode implementation.

REA-L-CH1 must be checked separately in the archive view with an explicit day:

```bash
/usr/bin/uenv run --view=rea-l-ch1 fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-REA-L-CH1 --date 20100101 --time 0000 \
  --max-step 1 --output-root /users/$USER/work/ptype-fdb-rea-l-smoke
```

Run this command with both algorithms. The focused live inventory check should
also verify all 80 full levels, 81 HHL half levels, and the three accumulated
grid-scale microphysics fields before release.

For local executable Fortran parity, use an ICON checkout that contains the
pinned commit:

```bash
PYTHONPATH=src python tools/verify_icon_fortran.py --icon-repo /path/to/icon-nwp
```

The verifier extracts `mo_diag_precip_type.f90` with `git show`, compiles that
exact blob with minimal dependency stubs, and runs the same deterministic raw
columns through Fortran and Python. It can refresh the committed frozen vectors
with `--write-vectors test/data/icon_fortran_reference.json`. The source SHA-256
is stored in the vector file, so a different upstream blob cannot be mistaken
for the reviewed reference.

## References

- Bourgouin, P. (2000): *A Method to Determine Precipitation Types*,
  `Weather and Forecasting`, 15(5), 583-592.
  [DOI](https://doi.org/10.1175/1520-0434%282000%29015%3C0583%3AAMTDPT%3E2.0.CO%3B2)
- Birk, K., E. Lenning, K. Donofrio, and M. T. Friedlein (2021):
  *A Revised Bourgouin Precipitation-Type Algorithm*,
  `Weather and Forecasting`, 36(2), 425-438.
  [DOI](https://doi.org/10.1175/WAF-D-20-0118.1)
