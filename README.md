# precip_type_diag

Categorical precipitation-type diagnostic for MeteoSwiss `ICON-CH1-EPS` and
`ICON-CH2-EPS`.

The production path reads the required model fields from realtime FDB on
Balfrin and writes one categorical `PTYPE` field per member and forecast hour,
plus `summary.json` and `monitoring.json`. GRIB2 is the default member output
format; NetCDF can be selected explicitly and is required for optional
probability products.

This repository intentionally contains only the FDB production path. There is
no file-based input mode and no bundled GRIB fixture data.

## References

The implementation follows Firdewsa Zukanovic's MSc thesis method,
*Precipitation Type Diagnostic for ICON*, which adapts the Modified Bourgouin
precipitation-type approach for ICON.

Core external references:

- Bourgouin, P. (2000): *A Method to Determine Precipitation Types*,
  `Weather and Forecasting`, 15(5), 583-592.
  https://doi.org/10.1175/1520-0434%282000%29015%3C0583%3AAMTDPT%3E2.0.CO%3B2

- Birk, K., E. Lenning, K. Donofrio, and M. T. Friedlein (2021):
  *A Revised Bourgouin Precipitation-Type Algorithm*,
  `Weather and Forecasting`, 36(2), 425-438.
  https://doi.org/10.1175/WAF-D-20-0118.1

- Code implemented during MSc thesis of Firdewsa
  https://github.com/MeteoSwiss-APN/precip_diagnostic

See [docs/science-and-architecture.md](docs/science-and-architecture.md) for
the implemented method, input/output contracts, and operational design.
See [docs/release-and-operations.md](docs/release-and-operations.md) for the
release gate, provenance, monitoring, and rollback expectations.
See [docs/provenance.md](docs/provenance.md) for licensing and source
provenance notes, and [docs/release-checklist.md](docs/release-checklist.md) for
the release-candidate checklist.

## Fresh Clone Setup

Use Python 3.11 or newer.

```bash
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[test]"
```

Check the checkout:

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
```

For handover or release-candidate checks, install the development extras and run
the full maintenance gate:

```bash
python -m pip install -e ".[test,dev]"
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
```

Local checks do not require FDB access. Running the production diagnostic does
require the Balfrin realtime FDB environment.

## Balfrin Setup

Choose a working directory on Balfrin, then clone the repository:

```bash
ssh balfrin
cd /users/$USER/work
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic
```

Create the runtime virtual environment with the realtime FDB uenv Python. The
uenv provides FDB, Earthkit, ecCodes, and NumPy; install only the missing local
runtime pieces into `.venv-fdb` so those uenv packages are not replaced:

```bash
uenv run --view=realtime fdb/5.18:v3 -- bash -lc '
  python -m venv --system-site-packages .venv-fdb
  .venv-fdb/bin/python -m pip install --upgrade pip setuptools wheel
  .venv-fdb/bin/python -m pip install "numba>=0.65,<0.66" "netCDF4>=1.7,<1.8"
  .venv-fdb/bin/python -m pip install --no-deps -e .
'
```

Run production commands inside that uenv and prepend its Python site-packages to
`PYTHONPATH`:

```bash
uenv image ls fdb
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag --help
```

If the available FDB image changes, replace `fdb/5.18:v3` with the current
realtime FDB image shown by `uenv image ls fdb`.

## Running

Production command for the latest complete `ICON-CH2-EPS` run:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members all \
  --output-root /users/$USER/work/ptype-fdb
```

Production command for `ICON-CH1-EPS`:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH1-EPS \
  --members all \
  --output-root /users/$USER/work/ptype-fdb
```

Small smoke test:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

DEPL-style production runs should pass an explicit cycle from the upstream
notification service and use the wrapper in `tools/`:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS 20260531 18 /users/$USER/work/ptype-fdb
```

The wrapper loads the realtime FDB uenv, uses all members, `--workers 8`,
`--chunk-size 2`, `--output-format netcdf`, `--write-probability-products`,
JSON INFO logs, and three bounded FDB retries. It does not submit to SLURM or
choose a queue; schedule it from DEPL or `sbatch` on a generally open partition
such as `pp-short` when the runtime fits below the queue limit.

Run a fixed FDB cycle instead of discovering the latest complete cycle:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --date 20260531 \
  --time 1800 \
  --max-step 3 \
  --output-root /users/$USER/work/ptype-fdb-fixed
```

Prefetching is enabled by default. Disable it only for debugging or comparison:

```bash
... python -m precip_type_diag ... --no-prefetch
```

Useful CLI options:

- `--members all` or `--members 000,001`
- `--start-step N` to choose the first diagnosed lead time; default is `1`
  because step 0 has no preceding hourly precipitation interval
- `--max-step N` to limit lead times for smoke tests
- `--workers N` for member-level process parallelism; default is `8`
- `--chunk-size N` for forecast-hour retrieval chunks
- `--summary-json /path/to/summary.json` for an extra summary copy
- `--monitoring-json /path/to/monitoring.json` for an extra machine-readable
  monitoring status copy
- `--run-id`, `--event-id`, and `--attempt` to record production trigger
  metadata in summaries and marker files
- `--log-level`, `--log-format text|json`, and `--log-file` for operational
  logging
- `--fdb-retries`, `--fdb-retry-initial-s`, and `--fdb-retry-max-s` for bounded
  retries of transient FDB list/retrieve/decode failures
- `--max-wall-s N` to make monitoring fail if wall-clock runtime exceeds `N`
  seconds
- `--output-format grib2|netcdf`; default is `grib2`
- `--no-output-file-check` to skip post-run existence checks for expected member
  output files
- `--write-probability-products` to write diagnostic member fields and strict
  all-member ensemble probability NetCDF products; requires
  `--output-format=netcdf`
- `--skip-input-checks` to skip FDB completeness checks
- `--precip-mask-threshold-mm X` to require at least `X` mm/h before diagnosing

## Outputs

The default GRIB2 output layout is:

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/<member>/lfffDDHHMMSS.ptype.grib2
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/summary.json
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/monitoring.json
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/DONE.json or FAILED.json
```

With `--output-format=netcdf`, the member output layout is:

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/<member>/lfffDDHHMMSS.ptype.nc
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/summary.json
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/monitoring.json
```

With `--output-format=netcdf --write-probability-products`, the member NetCDF
files include diagnostic variables and the run also writes:

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/probabilities/lfffDDHHMMSS.ptype_prob.nc
```

Member NetCDF files always contain `ptype`. When probability products are
enabled they also contain `hourly_precip_mm`, microphysics-consistent per-type
probabilities in percent, and thresholded hourly-precipitation fields using a
30% probability threshold and 0.01 mm/h precipitation mask. Final probability
NetCDF files contain ensemble means of those fields, categorical `PTYPE`
ensemble frequencies in percent, valid member count, and ensemble mean hourly
precipitation.

`summary.json` records:

- selected model, run date/time, members, worker count, chunk size, prefetch mode
- failed members, if any
- per-member output counts, active-column counts, retained vertical levels,
  forecast-hour chunk counts, FDB request counts, and timing breakdowns
- aggregate data-quality counters for non-finite precipitation, profile, and
  ground-temperature values
- runtime provenance: Python/platform metadata, dependency versions, Git commit,
  branch, dirty-worktree flag, and command-line arguments when available
- monitoring status and alerts
- aggregate timing fields for FDB checks, static-field retrieval, dynamic FDB
  requests split by field group, decode split by field group, diagnosis, and
  writing
- selected member output format
- production run metadata: run id, event id, attempt, hostname, user, PID, and
  SLURM job metadata when present
- retry policy and retry counters for FDB operations
- probability-product status, format, thresholds, product names, and output
  directory; enabled probability runs also report preflight, NetCDF read,
  aggregation, write, publish, and wall timings

`monitoring.json` is a compact status file for batch schedulers and dashboards.
It reports `status`, `ok`, `recommended_exit_code`, and critical alerts for
failed members, missing member results, incomplete member output counts,
fatal active-column data-quality counters, exceeded `--max-wall-s`, and missing
expected member output files. Requested probability-product failures are also
critical when `--write-probability-products` is used. The CLI returns the monitoring
`recommended_exit_code`, so critical monitoring alerts result in a non-zero
process exit.

Runs also write atomic state markers in the run directory. `RUNNING.json` is
created when member processing starts. It is replaced by `DONE.json` if
monitoring is OK, or by `FAILED.json` if monitoring is critical. Reruns replace
products, summaries, monitoring files, and markers atomically.

For a successful default full `ICON-CH2-EPS` run, expect `21 * 120 = 2520` GRIB
output files. Two measured CH2 step-1-to-24 Balfrin runtime matrices found
`--workers 8 --chunk-size 2` with default prefetching to be the fastest tested
GRIB2 configuration.

## Troubleshooting

- `fdb-utils` or FDB source errors usually mean the command is not running inside
  `uenv run --view=realtime fdb/...`.
- If Python cannot import FDB/earthkit support from the uenv, check that
  `PYTHONPATH` starts with
  `/user-environment/venvs/fdb/lib/python3.11/site-packages:src`.
- If ecCodes cannot resolve MeteoSwiss local parameters, run inside the FDB uenv
  or set `PRECIP_TYPE_DIAG_COSMO_DEFS` to the MeteoSwiss definitions directory.
- The package imports `eccodes` before `earthkit.data` in the FDB path because
  this ordering is required in some Balfrin realtime FDB environments.
