# precip_type_diag

Categorical precipitation-type diagnostic for MeteoSwiss `ICON-CH1-EPS` and
`ICON-CH2-EPS`.

The production path reads the required model fields from realtime FDB on
Balfrin and writes one categorical GRIB2 `PTYPE` field per member and forecast
hour, plus a `summary.json`.

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
  .venv-fdb/bin/python -m pip install "numba>=0.65,<0.66"
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
  --workers 4 \
  --output-root /users/$USER/work/ptype-fdb
```

Production command for `ICON-CH1-EPS`:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH1-EPS \
  --members all \
  --workers 4 \
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
- `--max-step N` to limit lead times for smoke tests
- `--workers N` for member-level process parallelism
- `--chunk-size N` for forecast-hour retrieval chunks
- `--summary-json /path/to/summary.json` for an extra summary copy
- `--monitoring-json /path/to/monitoring.json` for an extra machine-readable
  monitoring status copy
- `--max-wall-s N` to make monitoring fail if wall-clock runtime exceeds `N`
  seconds
- `--no-output-file-check` to skip post-run existence checks for expected GRIBs
- `--skip-input-checks` to skip FDB completeness checks
- `--precip-mask-threshold-mm X` to require at least `X` mm/h before diagnosing

## Outputs

The default output layout is:

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/<member>/lfffDDHHMMSS.ptype.grib2
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/summary.json
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/monitoring.json
```

`summary.json` records:

- selected model, run date/time, members, worker count, chunk size, prefetch mode
- failed members, if any
- per-member output counts, active-column counts, retained vertical levels, and
  timing breakdowns
- aggregate data-quality counters for non-finite precipitation, profile, and
  ground-temperature values
- runtime provenance: Python/platform metadata, dependency versions, Git commit,
  branch, dirty-worktree flag, and command-line arguments when available
- monitoring status and alerts
- aggregate timing fields for FDB requests, decode, diagnosis, and writing

`monitoring.json` is a compact status file for batch schedulers and dashboards.
It reports `status`, `ok`, `recommended_exit_code`, and critical alerts for
failed members, missing member results, incomplete member output counts,
fatal active-column data-quality counters, exceeded `--max-wall-s`, and missing
expected output GRIB files. The CLI returns the monitoring
`recommended_exit_code`, so critical monitoring alerts result in a non-zero
process exit.

For a successful full `ICON-CH2-EPS` run, expect `21 * 121 = 2541` GRIB output
files. A measured full CH2 run on Balfrin with `--workers 4` and default
prefetching took about 16 minutes wall-clock.

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
