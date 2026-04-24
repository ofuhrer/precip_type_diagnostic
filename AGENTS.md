# AGENTS.md

## Project Purpose

- `precip_type_diag` implements a precipitation-type diagnostic for MeteoSwiss `ICON-CH1-EPS` and `ICON-CH2-EPS`.
- It reproduces the MSc-thesis method by Firdewsa Zukanovic, based on the Modified Bourgouin algorithm described by Birk et al. (2021).
- Main user-facing outputs:
  - debug/member-hour processing from an `.../icon/<member>/lfff...` run directory
  - operational full-run processing from a MeteoSwiss cache root
  - one categorical GRIB2 `PTYPE` field per member and forecast hour
  - optional run summary JSON in operational mode
- Scope limits:
  - categorical output only
  - no ensemble aggregation product
  - no probability GRIB outputs
  - no bias correction or alternative diagnostic methods
- The production path is intentionally fixed:
  - fast ecCodes-based I/O
  - `numba` categorical backend
  - conservative static vertical truncation at `12 km` from `HHL`

## Repository Structure

- [README.md](/Users/fuhrer/Desktop/precip_type_diagnostic/README.md)
  - minimal install and run instructions
- [AGENTS.md](/Users/fuhrer/Desktop/precip_type_diagnostic/AGENTS.md)
  - this file
- [pyproject.toml](/Users/fuhrer/Desktop/precip_type_diagnostic/pyproject.toml)
  - package metadata and dependencies
- [pytest.ini](/Users/fuhrer/Desktop/precip_type_diagnostic/pytest.ini)
  - points pytest at `test/`
- [docs/initial-discovery.md](/Users/fuhrer/Desktop/precip_type_diagnostic/docs/initial-discovery.md)
  - cache layout, field IDs, output encoding decisions
- [docs/acceleration.md](/Users/fuhrer/Desktop/precip_type_diagnostic/docs/acceleration.md)
  - current fixed runtime path and benchmark commands
- [background/](/Users/fuhrer/Desktop/precip_type_diagnostic/background/)
  - thesis/prototype reference material
  - not part of the active production implementation

### Source Tree

- [src/precip_type_diag/constants.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/constants.py)
  - thresholds, GRIB param IDs, output codes
- [src/precip_type_diag/profile.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/profile.py)
  - single-column reference implementation
- [src/precip_type_diag/numba_backend.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/numba_backend.py)
  - accelerated categorical kernel
- [src/precip_type_diag/grid.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/grid.py)
  - grid-level preprocessing and categorical application
- [src/precip_type_diag/gribio.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/gribio.py)
  - GRIB discovery, ecCodes bootstrap, fast loader, output writing
- [src/precip_type_diag/operational.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/operational.py)
  - member-scoped batch runner and summary generation
- [src/precip_type_diag/verification.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/verification.py)
  - prototype regression and observation scoring helpers
- [src/precip_type_diag/benchmark.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/benchmark.py)
  - benchmark entrypoint
- [src/precip_type_diag/definitions/](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/definitions/)
  - local ecCodes overlay and code table extension for `PTYPE`
  - edit carefully; this affects GRIB decoding/writing

### Tests and Fixtures

- [test/test_profile.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_profile.py)
  - synthetic profile tests for the core algorithm
- [test/test_gribio.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_gribio.py)
  - loader, writer, metadata, mock fieldsets, real-fixture smoke tests
- [test/test_numba_backend.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_numba_backend.py)
  - parity tests between Python reference and `numba`
- [test/test_operational.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_operational.py)
  - operational runner behavior with mocks
- [test/test_verification.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_verification.py)
  - verification helpers
- [test/fixtures/](/Users/fuhrer/Desktop/precip_type_diagnostic/test/fixtures/)
  - large real GRIB fixtures
  - do not replace or expand casually; they materially affect repo size

### Generated Files / Directories

- `__pycache__/`, `*.pyc`
- `.pytest_cache/`
- `numba` cache files under `src/precip_type_diag/__pycache__/` such as `*.nbc` and `*.nbi`
- Operational outputs under the chosen output root, including `summary.json`
- Do not commit generated caches unless there is a specific reason.

## Development Workflow

### Install

```bash
python -m pip install .
```

For test dependencies:

```bash
python -m pip install -e .[test]
```

### Run the CLI

Single member/hour debug run:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-run /path/to/run/icon \
  --output-dir /path/to/output \
  --model ICON-CH2-EPS \
  --members 000 \
  --hours 04180000
```

Inspect a run without writing outputs:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-run /path/to/run/icon \
  --model ICON-CH2-EPS \
  --report-only
```

Operational full-run mode:

```bash
PYTHONPATH=src python -m precip_type_diag \
  --input-root /opr/osm/inn/cache \
  --output-root /path/to/output \
  --model ICON-CH2-EPS \
  --run latest
```

Benchmark:

```bash
PYTHONPATH=src python -m precip_type_diag.benchmark --case real-ch2
PYTHONPATH=src python -m precip_type_diag.benchmark --case synthetic
```

### Test / Validation

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
PYTHONPATH=src python -m pytest -q
```

- Formatter: Not currently defined.
- Linter: Not currently defined.
- Type checker: Not currently defined.
- CI config: Not currently defined in this repo.

### Environment Variables / External Tools

- `PRECIP_TYPE_DIAG_INPUT_ROOT`
  - default operational input root
- `PRECIP_TYPE_DIAG_OUTPUT_ROOT`
  - default operational output root
- `PRECIP_TYPE_DIAG_COSMO_DEFS`
  - override path to MeteoSwiss ecCodes definitions
- External runtime requirements:
  - `eccodes`
  - `eccodes-cosmo-resources-python`
  - `earthkit-data`
  - `numba`

## Coding Conventions

- Keep changes conservative and thesis-faithful unless fixing a clear bug.
- Prefer small patches over rewrites.
- The algorithmic reference is the pure Python column path in [profile.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/profile.py).
- The production path is allowed to optimize, but it must match the reference behavior for tested cases.
- Use dataclasses for structured inputs/results where the repo already does so.
- Keep functions typed; add type hints when touching existing code.
- Error handling style:
  - raise explicit exceptions (`ValueError`, `MissingFieldError`, `MissingFileError`)
  - do not add broad silent fallbacks
  - operational mode may record failures in summaries, but debug mode should fail loudly
- Logging:
  - there is no logging framework currently
  - CLI and operational mode communicate via returned/written JSON summaries
  - avoid adding ad hoc prints in library code
- Configuration:
  - prefer constants in [constants.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/constants.py)
  - avoid adding new runtime knobs unless necessary
- Naming:
  - meteorological fields use ICON/MeteoSwiss names like `T`, `P`, `QV`, `HHL`, `TOT_PREC`, `T_G`
  - step strings use ICON `DDHHMMSS` format
  - keep output category names aligned with `PrecipitationTypeCode`

## Testing Guidance

- Test framework: `pytest`
- Current test types:
  - synthetic unit tests for column behavior
  - mocked loader/writer tests
  - operational orchestration tests with fake scan functions
  - real-fixture smoke tests for CH1/CH2 GRIB data
  - parity tests for `numba`
- Add or update tests for every behavior-changing fix.
- Prefer synthetic tests for algorithm changes and mocked tests for orchestration changes.
- Use real fixtures only when the behavior genuinely depends on real GRIB encoding/metadata.
- Real-fixture tests may skip if the local ecCodes/MeteoSwiss definitions setup cannot decode the files.
- Important fixtures:
  - CH1: [test/fixtures/real_icon_ch1_eps](/Users/fuhrer/Desktop/precip_type_diagnostic/test/fixtures/real_icon_ch1_eps)
  - CH2: [test/fixtures/real_icon_ch2_eps](/Users/fuhrer/Desktop/precip_type_diagnostic/test/fixtures/real_icon_ch2_eps)
- Keep fixture growth under control; these files are very large.

## Domain-Specific Notes

- Input fields required by the implemented diagnostic:
  - `T`, `P`, `QV`, `HHL`, `TOT_PREC`, `T_G`
- Output:
  - one GRIB2 categorical `PTYPE` field
  - codes: `0, 1, 3, 5, 8, 12, 13`
- The forecast-step token in filenames is `DDHHMMSS`, not a plain hour count.
- Hourly precipitation is diagnosed as:
  - current `TOT_PREC` minus previous-hour `TOT_PREC`
- Vertical truncation:
  - fixed conservative cutoff at `12 km`
  - derived from `HHL`
  - same retained level range for the whole member/run
- The output writer uses a copied `TOT_PREC` message as template and rewrites parameter metadata and values.
- The ecCodes overlay extends code table `4.201` to include `13 = freezing_rain_on_ground`.
- The implementation is intentionally categorical-only even though the thesis/prototype had broader probability outputs.

## Safety and Maintenance Rules

- Do not change the scientific constants in [constants.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/constants.py) without a clear thesis-based reason and matching test updates.
- Do not change the output codes, `PTYPE` metadata, or code-table definitions casually; these affect downstream interoperability.
- Do not remove the pure Python reference path in [profile.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/profile.py) unless you replace its test role.
- Treat [src/precip_type_diag/definitions/](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/definitions/) as performance- and interoperability-sensitive.
- The performance-sensitive parts are:
  - fast GRIB scanning in `gribio`
  - categorical grid execution in `grid`
  - `numba` kernel in `numba_backend`
- Avoid adding unnecessary per-column Python work in the production path.
- Do not commit secrets, SSH material, or machine-specific cache paths.
- Avoid expanding real test fixtures unless necessary.

## Common Tasks

### Add or Change Diagnostic Logic

- Update the reference implementation first in [profile.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/profile.py).
- Then update the `numba` categorical equivalent in [numba_backend.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/numba_backend.py).
- Add or adjust:
  - synthetic tests in [test/test_profile.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_profile.py)
  - parity tests in [test/test_numba_backend.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_numba_backend.py)

### Change GRIB Input Handling

- Touch [gribio.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/gribio.py).
- Keep `load_member_hour()` behavior stable unless fixing a clear bug.
- Add mocked tests in [test/test_gribio.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_gribio.py).
- If metadata or decode behavior changes, add a real-fixture smoke test or update an existing one.

### Change Operational Behavior

- Touch [operational.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/operational.py).
- Preserve:
  - output layout
  - `summary.json` structure unless there is a compelling reason
  - idempotent output checks
  - atomic writes
- Update [test/test_operational.py](/Users/fuhrer/Desktop/precip_type_diagnostic/test/test_operational.py).

### Add a CLI Option

- Touch [__main__.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/__main__.py).
- Keep debug mode (`--input-run`) and operational mode (`--input-root`) behavior clearly separated.
- Validate user input early with `parser.error(...)`.
- Update README only if the option is user-facing.

### Debug Common Failures

- `RuntimeError` about MeteoSwiss ecCodes definitions:
  - check `eccodes-cosmo-resources-python`
  - check `PRECIP_TYPE_DIAG_COSMO_DEFS`
- Real-fixture test skips:
  - usually mean the local interpreter cannot decode MeteoSwiss GRIB fully
- Missing previous forecast file:
  - expected for first hour or incomplete latest runs
- Wrong valid-time assumptions:
  - re-check ICON step parsing as `DDHHMMSS`
- Slow performance:
  - benchmark with [benchmark.py](/Users/fuhrer/Desktop/precip_type_diagnostic/src/precip_type_diag/benchmark.py)
  - inspect GRIB I/O before touching the algorithm
