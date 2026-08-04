# precip_type_diag

`precip_type_diag` diagnoses the precipitation type at the surface for the
MeteoSwiss `ICON-CH1-EPS` and `ICON-CH2-EPS` ensemble forecasts and the
deterministic `ICON-REA-L-CH1` reanalysis.

For each requested member and forecast hour, it reads model fields
from MeteoSwiss FDB and writes one categorical `PTYPE` field. Every run also
writes a human- and machine-readable summary, monitoring status, and a final
run marker.

The project is intentionally narrow:

- input comes from the `realtime` or `rea-l-ch1` FDB view on Balfrin;
- member output is GRIB2 by default and can be NetCDF;
- optional NetCDF probability products are aggregated across every requested
  member;
- plotting, station postprocessing, and file-based input are out of scope.

Two scientific modes are available. `--algorithm firdewsa` is the default and
preserves the original thesis implementation. `--algorithm icon` selects the
adaptation aligned with ICON commit
[`50da7c5924994f7626688eb5185b8e66c781b12e`](https://gitlab.dkrz.de/icon/icon-nwp/-/commit/50da7c5924994f7626688eb5185b8e66c781b12e).
The selected mode and its fidelity limitations are recorded in `summary.json`.

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
diagnostic itself requires a Balfrin account, access to the selected FDB view,
and the MeteoSwiss FDB `uenv`.

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
- **FDB view**: the uenv configuration selecting either rolling forecasts
  (`realtime`) or the 2005–2025 reanalysis archive (`rea-l-ch1`).

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

Use the system `uenv` client. Version 8 or newer is required for the
Stackinator-v2 metadata in `fdb/5.21:v1`:

```bash
command -v uenv
/usr/bin/uenv --version
```

If `command -v uenv` reports a shell function from a user-installed
`activate-uenv`, disable that legacy activation or invoke `/usr/bin/uenv`
explicitly as shown below.

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- bash -lc '
  python -m venv .venv-fdb-5.21
  .venv-fdb-5.21/bin/python -m pip install --upgrade pip setuptools wheel
  .venv-fdb-5.21/bin/python -m pip install "numba>=0.66,<0.67" "netCDF4>=1.7,<1.8"
  .venv-fdb-5.21/bin/python -m pip install --no-deps -e .
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages \
    .venv-fdb-5.21/bin/python -m pip check
'
```

The FDB image stores its Python packages in its own virtual environment, so a
nested virtual environment cannot inherit them with `--system-site-packages`.
The documented `PYTHONPATH` exposes the uenv packages while the project venv
provides Numba and the editable package.

Confirm that the CLI starts in the FDB environment:

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag --help
```

`fdb/5.21:v1` is the reviewed production image for this release. If it is no
longer available, inspect `/usr/bin/uenv image ls`, select the currently
supported realtime image, and repeat both model smoke tests before promotion.

## First Balfrin Smoke Test

Start with one member and one forecast hour:

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

The command discovers the latest complete cycle. It succeeds when it exits with
code 0, `monitoring.json` contains `"ok": true`, and the run directory contains
`DONE.json`. See [Understanding a run](#understanding-a-run) for the layout.

To smoke-test the ICON-adapted path, append `--algorithm icon`. That mode also
checks and retrieves the archived `RAIN_GSP`, `SNOW_GSP`, and `GRAU_GSP`
accumulations.

### REA-L-CH1 day

REA-L-CH1 uses a separate FDB view and is deterministic. Select one archived
day explicitly; its only cycle is `0000` and its hourly steps are `0..24`:

```bash
/usr/bin/uenv run --view=rea-l-ch1 fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-REA-L-CH1 \
  --date 20100101 \
  --time 0000 \
  --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-rea-l-smoke
```

Run the same command once with the default Firdewsa mode and once with
`--algorithm icon`. The reanalysis contains all core fields and the three
archived grid-scale microphysics accumulations required by the offline
ICON-like mode.

`TOT_PREC`, `RAIN_GSP`, `SNOW_GSP`, and `GRAU_GSP` are accumulated from the
start of each daily 00 UTC cycle through step 24. The diagnostic differences
consecutive accumulations within that day and never carries an accumulation
across the day boundary. `--date` and `--time 0000` are therefore mandatory
for this model.

## Production Runs

### Latest complete cycle

This example runs every `ICON-CH2-EPS` member and forecast hour and writes GRIB2:

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members all \
  --output-root /users/$USER/work/ptype-fdb
```

Use `--model ICON-CH1-EPS` for CH1. The defaults are 21 members and 120
forecast hours for CH2, or 11 members and 33 forecast hours for CH1.
`ICON-REA-L-CH1` is a one-member, 24-hour daily dataset and must be run inside
the `rea-l-ch1` view with an explicit date and `--time 0000`.

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
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
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
  provenance. It also records `diagnostic_algorithm`, the effective
  precipitation mask, and `algorithm_fidelity`.
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
| `6` | wet snow (ICON mode) |
| `7` | mixture of rain and snow (ICON mode) |
| `8` | ice pellets |
| `9` | graupel (ICON mode) |
| `10` | hail (ICON mode; requires a supplied hail rate) |
| `12` | freezing drizzle |
| `13` | freezing rain on ground |

## Common Options

Run `python -m precip_type_diag --help` for the complete CLI reference. The
options most useful for first runs are:

- `--members all` or `--members 000,001`
- `--model ICON-REA-L-CH1` selects the deterministic reanalysis; only member
  `000` is valid and `--date YYYYMMDD --time 0000` are required
- `--algorithm firdewsa|icon`; default `firdewsa`
- `--max-step N` to shorten a smoke test
- `--workers N` to change member-level parallelism; default `8`
- `--output-format grib2|netcdf`; default `grib2`
- `--write-probability-products`; requires `--output-format netcdf`
- `--max-wall-s N` to make an overlong run fail monitoring
- `--log-format text|json` and `--log-file PATH`
- `--no-prefetch` for debugging or performance comparison
- `--skip-input-checks` only when deliberately bypassing FDB completeness checks

The default precipitation mask is mode-specific: `0.0 mm` for `firdewsa` and
the ICON trace threshold of `0.01 mm` for the one-hour production interval.
An explicit `--precip-mask-threshold-mm` overrides either default.

## Troubleshooting

- **`KeyError: 'activate'` when starting `fdb/5.21`:** a legacy user-installed
  `uenv` is shadowing `/usr/bin/uenv`; use the system client version 8 or newer.
- **`fdb-utils` or FDB source errors:** confirm the command is inside
  `/usr/bin/uenv run --view=realtime fdb/...`.
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
