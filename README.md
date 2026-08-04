# precip_type_diag

`precip_type_diag` diagnoses the precipitation type at the surface for the
MeteoSwiss `ICON-CH1-EPS` and `ICON-CH2-EPS` ensemble models.

For each requested ensemble member and forecast hour, it reads model fields
from MeteoSwiss FDB and writes one categorical `PTYPE` field. Every run also
writes a human- and machine-readable summary, monitoring status, and a final
run marker.

The project is intentionally narrow:

- production input comes only from the realtime FDB on Balfrin;
- member output is GRIB2 by default and can be NetCDF;
- optional NetCDF probability products are aggregated across every requested
  member;
- plotting, station postprocessing, and file-based input are out of scope.

## Start Here

Choose the path that matches what you need:

| Goal | Start with |
| --- | --- |
| Understand or change the code | [Local development setup](#local-development-setup) |
| Check that FDB access works | [First Balfrin smoke test](#first-balfrin-smoke-test) |
| Run a production cycle | [Production runs](#production-runs) |
| Understand the diagnostic | [Science and architecture](docs/science-and-architecture.md) |
| Prepare or operate a release | [Release and operations](docs/release-and-operations.md) |

A local checkout can run all automated checks without FDB access. Running the
diagnostic itself requires a Balfrin account, access to the realtime FDB, and
the MeteoSwiss FDB `uenv`.

## Key Terms

- **FDB**: the forecast database that supplies the ICON model fields.
- **Cycle**: one model initialization, identified by date and time, for example
  `20260531/1800`.
- **Member**: one ensemble realization. Member `000` is the control member.
- **Forecast step**: lead time in hours from the model cycle. Production starts
  at step 1 because hourly precipitation needs both the current and previous
  accumulated-precipitation fields.
- **Balfrin**: the MeteoSwiss system on which the realtime FDB environment is
  available.

## Local Development Setup

Use Python 3.11 or newer:

```bash
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[test,dev]"
```

Run the same maintenance checks used for handover and releases:

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
```

None of these commands contacts FDB. The test suite uses synthetic data and
mocked orchestration.

## Balfrin Runtime Setup

Clone the repository in a working directory on Balfrin:

```bash
ssh balfrin
cd /users/$USER/work
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic
```

Create a virtual environment inside the realtime FDB `uenv`. The uenv supplies
FDB, Earthkit, ecCodes, and NumPy; the commands below add the remaining runtime
packages without replacing the uenv versions:

```bash
uenv run --view=realtime fdb/5.18:v3 -- bash -lc '
  python -m venv --system-site-packages .venv-fdb
  .venv-fdb/bin/python -m pip install --upgrade pip setuptools wheel
  .venv-fdb/bin/python -m pip install "numba>=0.65,<0.66" "netCDF4>=1.7,<1.8"
  .venv-fdb/bin/python -m pip install --no-deps -e .
'
```

Confirm that the CLI starts in the FDB environment:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag --help
```

If `fdb/5.18:v3` is no longer available, use `uenv image ls fdb` to find the
current realtime image and record the selected version with the run.

## First Balfrin Smoke Test

Start with one member and one forecast hour:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

The command discovers the latest complete cycle. It succeeds when it exits with
code 0, `monitoring.json` contains `"ok": true`, and the run directory contains
`DONE.json`. See [Understanding a run](#understanding-a-run) for the layout.

## Production Runs

### Latest complete cycle

This example runs every `ICON-CH2-EPS` member and forecast hour and writes GRIB2:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members all \
  --output-root /users/$USER/work/ptype-fdb
```

Use `--model ICON-CH1-EPS` for CH1. The defaults are 21 members and 120
forecast hours for CH2, or 11 members and 33 forecast hours for CH1.

### Explicit production cycle with probability products

For scheduler- or DEPL-triggered runs, pass the cycle explicitly through the
provided wrapper:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS 20260531 18 /users/$USER/work/ptype-fdb
```

The wrapper accepts `HH` or `HHMM`, loads the realtime FDB uenv, and runs all
members with the operational defaults: 8 workers, forecast-hour chunks of 2,
NetCDF member output, probability products, JSON INFO logs, and three bounded
FDB retries. Extra CLI arguments can be appended to the command.

The wrapper does not submit a job or select a SLURM partition. That remains the
responsibility of DEPL or the calling scheduler. Guidance and an `sbatch`
example are in [Release and operations](docs/release-and-operations.md).

### Explicit cycle without the wrapper

Use `--date YYYYMMDD` and `--time HHMM` together:

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

## Understanding a Run

Outputs are grouped by model and cycle:

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/
├── <member>/
│   └── lfffDDHHMMSS.ptype.grib2  # or .ptype.nc
├── probabilities/                # only when probability products are enabled
│   └── lfffDDHHMMSS.ptype_prob.nc
├── summary.json
├── monitoring.json
└── DONE.json                     # FAILED.json when monitoring is critical
```

During processing, `RUNNING.json` is present instead of the final marker.

- `summary.json` contains the selected cycle and members, output counts,
  failures, data-quality counters, timings, retry statistics, and runtime/Git
  provenance.
- `monitoring.json` is the scheduler contract. Its `ok`, `status`, alerts, and
  `recommended_exit_code` fields summarize whether the run is usable.
- The CLI exits non-zero when monitoring reports a critical condition.

Member NetCDF files always contain `ptype`. When probability products are
enabled, they also contain hourly precipitation and per-type diagnostic
probabilities. Final probability files contain ensemble means, categorical
frequencies, valid-member count, and mean hourly precipitation. Probability
values use a `0..100` percent scale.

The categorical `PTYPE` codes are:

| Code | Meaning |
| ---: | --- |
| `0` | no precipitation |
| `1` | rain |
| `3` | freezing rain |
| `5` | snow |
| `8` | ice pellets |
| `12` | freezing drizzle |
| `13` | freezing rain on ground |

## Common Options

Run `python -m precip_type_diag --help` for the complete CLI reference. The
options most useful for first runs are:

- `--members all` or `--members 000,001`
- `--max-step N` to shorten a smoke test
- `--workers N` to change member-level parallelism; default `8`
- `--output-format grib2|netcdf`; default `grib2`
- `--write-probability-products`; requires `--output-format netcdf`
- `--max-wall-s N` to make an overlong run fail monitoring
- `--log-format text|json` and `--log-file PATH`
- `--no-prefetch` for debugging or performance comparison
- `--skip-input-checks` only when deliberately bypassing FDB completeness checks

## Troubleshooting

- **`fdb-utils` or FDB source errors:** confirm the command is inside
  `uenv run --view=realtime fdb/...`.
- **Python cannot import FDB or Earthkit:** confirm `PYTHONPATH` begins with
  `/user-environment/venvs/fdb/lib/python3.11/site-packages:src`.
- **ecCodes cannot resolve the local `PTYPE` parameter:** run inside the FDB
  uenv, or set `PRECIP_TYPE_DIAG_COSMO_DEFS` to the MeteoSwiss definitions
  directory.
- **A run exits non-zero:** read `monitoring.json` first, then use
  `summary.json["failed"]`, the alerts, and the run log to find the failing
  member or stage.
- **A rerun finds old probability files:** probability publication replaces the
  run's complete `probabilities/` directory; a failed publication removes it.

## Further Documentation

- [Science and architecture](docs/science-and-architecture.md): method, input
  fields, category contract, implementation, and test strategy.
- [Release and operations](docs/release-and-operations.md): release gate,
  deployment, monitoring, and rollback.
- [Release checklist](docs/release-checklist.md): fill-in record for a release
  candidate.
- [Provenance and licensing](docs/provenance.md): scientific sources, bundled
  references, ecCodes definitions, and redistribution considerations.

The implementation follows Firdewsa Zukanovic's MSc thesis method based on the
Modified Bourgouin algorithm. The principal references are Bourgouin (2000),
Birk et al. (2021), and the
[MeteoSwiss thesis prototype](https://github.com/MeteoSwiss-APN/precip_diagnostic).
