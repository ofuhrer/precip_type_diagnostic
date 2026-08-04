# Science and Architecture

## Scope

`precip_type_diag` produces one categorical precipitation-type field per ICON
ensemble member and forecast hour for:

- `ICON-CH1-EPS`
- `ICON-CH2-EPS`

By default the package writes categorical member `PTYPE` GRIB2 outputs. With
`--output-format=netcdf`, it writes member `PTYPE` NetCDF outputs instead. With
`--output-format=netcdf --write-probability-products`, those member NetCDF files
also contain diagnostic fields and the run writes strict all-member ensemble
probability NetCDF products. It does not produce plotting, bias correction,
station postprocessing, or alternative diagnostics.

## Scientific Method

The implementation follows Firdewsa Zukanovic's MSc thesis,
*Precipitation Type Diagnostic for ICON*. The method is based on the Modified
Bourgouin precipitation-type algorithm and uses ICON model columns to diagnose a
categorical surface precipitation type.

The broader method lineage is:

- Bourgouin (2000): original area method using melting and refreezing energy
  from the vertical thermal profile.
- Birk et al. (2021): revised/Modified Bourgouin method, including wet-bulb
  profile usage and ice-nucleation considerations.
- Zukanovic MSc thesis: ICON/MeteoSwiss implementation choices mirrored by this
  repository. See https://github.com/MeteoSwiss-APN/precip_diagnostic for code.

## Input Fields

The FDB production path fetches only these fields:

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

## Column Algorithm

The pure Python reference implementation is in `profile.py`.

For each active column:

1. Convert temperature to Celsius.
2. Derive dew point, wet-bulb temperature, and relative humidity over ice.
3. Identify precipitating and sublimating layers.
4. Estimate ice probability from the precipitation-generation layer.
5. Compute melting and refreezing energies from the wet-bulb profile.
6. Convert the resulting probabilities to one categorical code by selecting the
   highest-probability type, using the fixed priority order in `constants.py`
   only for ties.

The production grid path in `grid.py` uses the same logic through the numba
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

## Output Codes

The categorical output is encoded as MeteoSwiss `PTYPE`:

| Code | Meaning |
| ---: | --- |
| `0` | no precipitation |
| `1` | rain |
| `3` | freezing rain |
| `5` | snow |
| `8` | ice pellets |
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
  -> optionally aggregate strict all-member NetCDF probability products
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
| CH1 max step | `33` |
| CH2 max step | `120` |
| start step | `1` |
| worker count | `8` unless overridden |
| chunk size | `2` forecast hours |
| prefetch | enabled |
| output format | `grib2` |
| vertical cutoff | `12000 m` |
| precipitation mask threshold | `0.0 mm/h` |

The vertical cutoff is derived from `HHL`; levels above the cutoff are discarded
before diagnosis. The cutoff is a performance optimization and should not be
changed without scientific review.

## Test Strategy

The test suite has three layers:

- `test_profile.py` and `test_numba_backend.py`: science/algorithm parity checks.
- `test_grid.py`: grid data-quality behavior for dry and active columns.
- `test_probabilities.py`: NetCDF member output, aggregation, and strict completeness
  checks.
- `test_operational.py` and `test_cli.py`: mocked FDB orchestration and CLI
  behavior.

Real FDB access is checked manually on Balfrin with a smoke run, for example:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

Formal releases should rerun the smoke test from the annotated release tag.

## References

- Bourgouin, P. (2000): *A Method to Determine Precipitation Types*,
  `Weather and Forecasting`, 15(5), 583-592.
  https://doi.org/10.1175/1520-0434%282000%29015%3C0583%3AAMTDPT%3E2.0.CO%3B2
- Birk, K., E. Lenning, K. Donofrio, and M. T. Friedlein (2021):
  *A Revised Bourgouin Precipitation-Type Algorithm*,
  `Weather and Forecasting`, 36(2), 425-438.
  https://doi.org/10.1175/WAF-D-20-0118.1
