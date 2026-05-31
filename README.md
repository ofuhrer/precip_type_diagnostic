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
See [docs/scientific-validation.md](docs/scientific-validation.md) for the
required real-case validation evidence and manifest formats for operational
acceptance.
See [docs/release-and-operations.md](docs/release-and-operations.md) for the
release gate, provenance, monitoring, and rollback expectations.

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

Validate the checkout:

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
```

Local validation does not require FDB access. Running the production diagnostic
does require the Balfrin realtime FDB environment.

## Balfrin Setup

Choose a working directory on Balfrin, then clone and create a local virtual
environment:

```bash
ssh balfrin
cd /users/$USER/work
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic

python3 -m venv .venv-fdb
.venv-fdb/bin/python -m pip install --upgrade pip setuptools wheel
.venv-fdb/bin/python -m pip install -e ".[test]"
```

The FDB access libraries are provided by the realtime FDB uenv. Run production
commands inside that uenv and prepend its Python site-packages to `PYTHONPATH`:

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
- `--skip-validation` to skip FDB completeness validation
- `--precip-mask-threshold-mm X` to require at least `X` mm/h before diagnosing

## Outputs

The default output layout is:

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/<member>/lfffDDHHMMSS.ptype.grib2
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/summary.json
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
- aggregate timing fields for FDB requests, decode, diagnosis, and writing

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
