# precip_type_diag

`precip_type_diag` computes categorical surface precipitation type from
MeteoSwiss ICON fields in FDB. It supports the realtime `ICON-CH1-EPS` and
`ICON-CH2-EPS` forecasts and deterministic `ICON-REA-L-CH1` reanalysis.

Two algorithms are available:

- `firdewsa` (default) preserves Firdewsa Zukanovic's MSc implementation of the
  Modified Bourgouin method.
- `icon` follows ICON commit
  [`50da7c5924994f7626688eb5185b8e66c781b12e`](https://gitlab.dkrz.de/icon/icon-nwp/-/commit/50da7c5924994f7626688eb5185b8e66c781b12e)
  as closely as the archived inputs allow.

Each requested member and hour produces one `PTYPE` field. GRIB2 is the default;
NetCDF is available and is required for strict all-member probability products.
Runs also write a summary, monitoring status, and an atomic run-state marker.

## Supported Data

| Model | FDB view | Members | Steps | Cycle selection |
| --- | --- | ---: | ---: | --- |
| `ICON-CH1-EPS` | `realtime` | `000..010` | `1..33` | latest complete or explicit |
| `ICON-CH2-EPS` | `realtime` | `000..020` | `1..120` | latest complete or explicit |
| `ICON-REA-L-CH1` | `rea-l-ch1` | `000` | `1..24` | explicit date and `0000` |

Accumulated fields in realtime start at the forecast cycle. REA-L-CH1 fields
start at each daily 00 UTC cycle and run through step 24. The diagnostic always
differences adjacent steps within one cycle and never carries an REA
accumulation across days.

The tool is FDB-only and runs operationally on Balfrin. File input, plotting,
station postprocessing, and bias correction are out of scope.

## Local Development

Python 3.11 or newer is required:

```bash
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[test,dev]"
```

Run the complete local gate:

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
git diff --check
```

These checks use synthetic data and mocked orchestration; they do not contact FDB.

## Balfrin Setup

Use the system `uenv` client (version 8 or newer) and the reviewed FDB image
`fdb/5.21:v1`:

```bash
ssh balfrin
cd /users/$USER/work
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic
/usr/bin/uenv --version

/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- bash -lc '
  python -m venv .venv-fdb-5.21
  .venv-fdb-5.21/bin/python -m pip install --upgrade pip setuptools wheel
  .venv-fdb-5.21/bin/python -m pip install "numba>=0.66,<0.67" "netCDF4>=1.7,<1.8"
  .venv-fdb-5.21/bin/python -m pip install --no-deps -e .
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages \
    .venv-fdb-5.21/bin/python -m pip check
'
```

The FDB image keeps Python packages in its own virtual environment, so a nested
venv cannot inherit them through `--system-site-packages`. Keep the documented
FDB site-packages first on `PYTHONPATH`. If a legacy `activate-uenv` shadows the
system client, invoke `/usr/bin/uenv` explicitly.

Confirm the CLI starts:

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag --help
```

If the reviewed image is unavailable, select a supported image with
`/usr/bin/uenv image ls` and repeat all relevant live smoke tests before use.

## Smoke Tests

### Realtime forecast

This discovers the latest complete CH2 cycle and processes one member/hour:

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS --members 000 --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

Repeat with `ICON-CH1-EPS`. Append `--algorithm icon` to exercise the ICON-like
path and its required `RAIN_GSP`, `SNOW_GSP`, and `GRAU_GSP` accumulations.

### REA-L-CH1

REA is deterministic and must use an explicit archived day and `time=0000`:

```bash
/usr/bin/uenv run --view=rea-l-ch1 fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-REA-L-CH1 --date 20100101 --time 0000 --max-step 1 \
  --output-root /users/$USER/work/ptype-fdb-rea-l-smoke
```

Run once per algorithm. A successful smoke exits with code 0, writes
`DONE.json`, and records `"ok": true` in `monitoring.json`.

## Production Runs

Run all members and default steps for the latest complete realtime cycle:

```bash
/usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb-5.21/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS --members all \
  --output-root /users/$USER/work/ptype-fdb
```

For a scheduler-selected realtime cycle, use the DEPL wrapper:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS 20260531 18 /users/$USER/work/ptype-fdb
```

The wrapper accepts `HH` or `HHMM` and selects all members, 8 workers, 2-hour
chunks, NetCDF, probability products, JSON logs, and three FDB retries. It is
realtime-only and does not submit to SLURM or choose a partition. Invoke the
module directly for REA-L-CH1. See
[Release and operations](docs/release-and-operations.md) for scheduling,
promotion, monitoring, and rollback.

## Outputs

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/
├── <member>/lfffDDHHMMSS.ptype.grib2  # or .ptype.nc
├── probabilities/                     # requested NetCDF probabilities only
├── summary.json
├── monitoring.json
└── DONE.json                           # FAILED.json on critical status
```

`RUNNING.json` exists while the run is active. `summary.json` records selection,
output and quality counts, failures, timings, retries, algorithm fidelity, FDB
source, and runtime/Git provenance. `monitoring.json` is the scheduler contract;
critical alerts produce a non-zero CLI exit.

Probability products are strict across every requested member and use a
`0..100` percent scale. Member NetCDF always contains `ptype`; probability runs
also store the required diagnostic variables.

## Useful Options

Run `python -m precip_type_diag --help` for the complete reference. Common options:

- `--algorithm firdewsa|icon` (default: `firdewsa`)
- `--members all|000,001`
- `--date YYYYMMDD --time HHMM`
- `--start-step N --max-step N`
- `--workers N --chunk-size N --no-prefetch`
- `--output-format grib2|netcdf`
- `--write-probability-products` (requires NetCDF)
- `--max-wall-s N`
- `--log-format text|json --log-file PATH`
- `--skip-input-checks` only for deliberate completeness-check bypasses

The default precipitation mask is `0.0 mm` for Firdewsa and `0.01 mm` for the
hourly ICON mode. `--precip-mask-threshold-mm` overrides it.

## Troubleshooting

- **`KeyError: 'activate'` from `uenv`:** use `/usr/bin/uenv` version 8 or newer.
- **FDB errors:** verify the correct view (`realtime` or `rea-l-ch1`) and image.
- **Python cannot import FDB/Earthkit:** put the documented FDB site-packages
  first on `PYTHONPATH`.
- **ecCodes cannot resolve `PTYPE`:** run in the FDB uenv or set
  `PRECIP_TYPE_DIAG_COSMO_DEFS` to the MeteoSwiss definitions directory.
- **Non-zero run exit:** inspect `monitoring.json`, then `summary.json["failed"]`
  and the run log.

## Documentation

- [Science and architecture](docs/science-and-architecture.md): algorithms,
  fields, FDB contracts, categories, and implementation.
- [Release and operations](docs/release-and-operations.md): deployment,
  monitoring, and rollback.
- [Release checklist](docs/release-checklist.md): release evidence template.
- [Provenance and licensing](docs/provenance.md): scientific sources and
  redistribution notes.
