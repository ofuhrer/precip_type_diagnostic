# precip_type_diag

`precip_type_diag` is an ICON precipitation-type diagnostic for `ICON-CH1-EPS` and
`ICON-CH2-EPS`. It implements the MeteoSwiss MSc-thesis method by Firdewsa
Zukanovic, based on the Modified Bourgouin precipitation-type algorithm described
by Birk et al. (2021).

## Installation

```bash
python -m pip install .
```

This installs the package together with the required `numba`, `eccodes`, and
MeteoSwiss ecCodes definition resources.

## Test Fixtures

The repository does not check in the large real GRIB test fixtures. Fetch them
from `tasna` with:

```bash
./scripts/fetch_test_fixtures.sh
```

This copies the current CH1/CH2 fixture set into `test/fixtures/`. The script
skips files that already exist locally and discovers a suitable live cache run
on `tasna` automatically. The fetched files are ignored by git.

The fetch location can be customized:

```bash
REMOTE_HOST=tasna REMOTE_CACHE_ROOT=/opr/osm/inn/cache ./scripts/fetch_test_fixtures.sh
./scripts/fetch_test_fixtures.sh /tmp/precip_type_diag_fixtures
```

Real-fixture tests skip automatically when the required files are not present.

## Running

Process a single member/hour from one ICON run:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-run /path/to/run/icon \
  --output-dir /path/to/output \
  --model ICON-CH2-EPS \
  --members 000 \
  --hours 04180000
```

Process one full operational run:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-root /opr/osm/inn/cache \
  --output-root /path/to/output \
  --model ICON-CH2-EPS \
  --run latest
```

Operational mode writes one `summary.json` under the output run directory. The
summary includes the model, resolved run id, input and output paths, runtime
configuration, member list, total written/skipped/failed counts, merged category
counts, total runtime, and one per-member summary. Each member summary contains
`written`, `skipped`, `failed`, `category_counts`, and `runtime_s`.

An extra copy of the same summary can be written with `--summary-json`.

## Validation

Run the synthetic and mocked tests without requiring real fixtures:

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
PYTHONPATH=src python -m pytest -q
```

When real fixtures have been fetched, the CH1/CH2 GRIB smoke tests also run.
