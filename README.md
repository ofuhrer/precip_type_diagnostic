# precip_type_diag

ICON precipitation-type diagnostic for MeteoSwiss `ICON-CH1-EPS` and
`ICON-CH2-EPS`, following the Zukanovic MSc-thesis implementation of the
Modified Bourgouin method.

The package writes one categorical GRIB2 `PTYPE` field per member and forecast
hour. It is categorical-only: no ensemble aggregation, probability products, or
bias correction.

## Install

```bash
python -m pip install .
python -m pip install -e ".[test]"
```

## Tasna

Use the module Python on `tasna`; the system Python may lack required extension
modules.

```bash
module use /mch-environment/v8/modules
module load python/3.11.7

cd /users/olifu/work
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic

python -m venv .venv
. .venv/bin/activate
python -m ensurepip --upgrade
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[test]"
```

If GitHub clone fails, configure SSH or HTTPS credentials first.

## Run

Inspect a run:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-run /path/to/run/icon \
  --model ICON-CH2-EPS \
  --report-only
```

Process one member/hour:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-run /path/to/run/icon \
  --output-dir /path/to/output \
  --model ICON-CH2-EPS \
  --members 000 \
  --hours 04180000
```

Process an operational run:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-root /opr/osm/inn/cache \
  --output-root /path/to/output \
  --model ICON-CH2-EPS \
  --run latest
```

Operational mode writes `summary.json` under the output run directory. Use
`--summary-json` to write an extra copy.

## Fixtures

Large real GRIB fixtures are not tracked. Fetch them from `tasna` when needed:

```bash
./scripts/fetch_test_fixtures.sh
```

Optional customizations:

```bash
REMOTE_HOST=tasna REMOTE_CACHE_ROOT=/opr/osm/inn/cache ./scripts/fetch_test_fixtures.sh
./scripts/fetch_test_fixtures.sh /tmp/precip_type_diag_fixtures
```

Real-fixture tests skip when the files are absent.

## Validate

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
PYTHONPATH=src python -m pytest -q
```

Benchmark:

```bash
PYTHONPATH=src python -m precip_type_diag.benchmark --case real-ch2
PYTHONPATH=src python -m precip_type_diag.benchmark --case synthetic
```

## Notes

- Required input fields: `T`, `P`, `QV`, `HHL`, `TOT_PREC`, `T_G`.
- Forecast steps use ICON `DDHHMMSS` strings.
- Hourly precipitation is `TOT_PREC(current) - TOT_PREC(previous)`.
- The production path uses ecCodes I/O, a `numba` categorical backend, and a
  fixed 12 km vertical cutoff from `HHL`.
- GRIB index files are cached under the system temp directory by default. Set
  `PRECIP_TYPE_DIAG_GRIB_INDEX_CACHE=/path/to/cache` to choose a location, or
  `PRECIP_TYPE_DIAG_GRIB_INDEX_CACHE=off` to disable persisted caching. Cached
  `.idx` files older than 10 days are pruned best-effort.
