# Release and Operations

This guide is for the person preparing, deploying, or supporting an operational
release. A release is ready only when four things are recorded together:

1. the exact code revision;
2. the Python and FDB dependency environment;
3. successful automated checks and Balfrin smoke tests;
4. an executable rollback plan.

Use [release-checklist.md](release-checklist.md) as the release-candidate record
template rather than collecting this evidence informally. Use
[provenance.md](provenance.md) for source and licensing notes. Setup and first
run instructions are in the [README](../README.md).

## Pre-Release Gate

Before tagging a release:

1. Run local checks:

   ```bash
   python -m pip install -e ".[test,dev]"
   python -m py_compile src/precip_type_diag/*.py test/*.py
   python -m ruff check .
   python -m mypy
   PYTHONPATH=src python -m pytest -q
   PYTHONPATH=src python -m precip_type_diag.benchmark
   python -m pip check
   ```

   When the ICON-adapted science changes, also execute the pinned Fortran
   comparison against an ICON checkout:

   ```bash
   PYTHONPATH=src python tools/verify_icon_fortran.py --icon-repo /path/to/icon-nwp
   ```

2. Confirm the GitHub Actions `tests` workflow passes for the release branch.
3. Run a Balfrin FDB smoke test for each operational model:

   ```bash
   /usr/bin/uenv run --view=realtime fdb/5.21:v1 -- \
     env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
     .venv-fdb-5.21/bin/python -m precip_type_diag \
     --model ICON-CH2-EPS \
     --members 000 \
     --max-step 1 \
     --max-wall-s 900 \
     --output-root /users/$USER/work/ptype-fdb-smoke
   ```

   The command shows CH2. Repeat it with `--model ICON-CH1-EPS` and a separate
   output directory. For dual-mode science or FDB changes, repeat both model
   checks with `--algorithm icon` and confirm the archived rain, snow, and
   graupel accumulations pass completeness checks. The
   [release checklist](release-checklist.md) provides a loop that runs both models.

   The tested `fdb/5.21:v1` setup uses the system `/usr/bin/uenv` client and a
   separate `.venv-fdb-5.21` for Numba 0.66 while exposing the FDB image's
   Python packages first on `PYTHONPATH`.

4. Re-read at least one smoke-test member output and check `PTYPE` metadata,
   shape, and allowed category codes. For the default smoke test this is a GRIB2
   file; for `--output-format=netcdf`, inspect the NetCDF `ptype` variable.
5. Confirm `monitoring.json["ok"]` is `true` and archive `summary.json`,
   `monitoring.json`, command output, and data owner approval with the release
   decision.

## Versioning

Use annotated Git tags for released code:

```bash
git tag -a vX.Y.Z -m "precip_type_diag vX.Y.Z"
git push origin vX.Y.Z
```

The package version in `pyproject.toml` must be updated for any release
candidate or accepted production release. The operational summary records:

- Python implementation and version;
- operating system summary;
- package versions for the runtime dependencies;
- Git commit, branch, and dirty-worktree flag when available;
- command-line arguments;
- selected diagnostic algorithm, effective trace threshold, and known ICON
  archived-microphysics fidelity limits.

Do not promote output generated from a dirty worktree unless the exact diff is
archived and approved.

The repository source code is licensed under the BSD 3-Clause License in
`LICENSE`. Confirm redistribution rights for bundled background PDFs before any
external release or public artifact publication that includes `background/`.

## Deployment

The production path is the module or console entry point:

```bash
python -m precip_type_diag ...
precip-type-diag ...
```

Run inside the documented realtime FDB `uenv` and keep the `uenv` image version
with the release record. If the FDB image changes, rerun smoke tests before
promotion.

For DEPL-triggered production, keep cycle selection outside the diagnostic. The
notification service should call the explicit wrapper with model, date, time,
and output root:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS 20260531 18 /users/$USER/work/ptype-fdb
```

The wrapper runs the Python module with explicit options for the operational
product set: all members, `--workers 8`, `--chunk-size 2`,
`--output-format netcdf`, `--write-probability-products`, JSON INFO logging,
and three bounded FDB retries. It intentionally does not submit to SLURM or
choose a partition; scheduling remains owned by DEPL.

For manual Balfrin SLURM smoke, benchmark, or validation jobs, use the generally
open service-node partition `pp-short` when the expected runtime fits below its
one-hour limit. Avoid elevated-rights partitions such as `pp-production`,
`pp-prodntc`, and `pp-dispntc` for development or benchmarking runs; they are
restricted by group and should not be the default for this project.

Minimal `sbatch` wrapper for a manually submitted DEPL-style cycle:

```bash
#!/usr/bin/env bash
#SBATCH --job-name=ptype-diag
#SBATCH --partition=pp-short
#SBATCH --time=00:59:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16

set -euo pipefail
cd /users/$USER/work/precip_type_diagnostic
tools/run_depl_cycle.sh "$MODEL" "$DATE" "$TIME" "$OUTPUT_ROOT"
```

For `fdb/5.21:v1`, use `/usr/bin/uenv` version 8 or newer and create
`.venv-fdb-5.21` inside the uenv. Install Numba 0.66 and this package with
`--no-deps`, then run `pip check` with the documented FDB site-packages on
`PYTHONPATH`. The FDB image's Python environment cannot be inherited by a
nested venv through `--system-site-packages`. This arrangement preserves the
reviewed FDB uenv stack—Python 3.11, NumPy 2.4, Earthkit 1.0, ecCodes 2.47, and
NetCDF4 1.7—while adding the accelerated diagnostic backend. A legacy
user-installed `activate-uenv` must not shadow `/usr/bin/uenv`.

## Monitoring

Every run writes:

- `<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/summary.json`
- `<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/monitoring.json`
- `<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/RUNNING.json`, then either
  `DONE.json` or `FAILED.json`

`monitoring.json` is the scheduler/dashboard contract. It contains `status`,
`ok`, `recommended_exit_code`, observed/expected counts, and critical alerts for:

- non-empty `summary.json["failed"]`;
- requested members with no processed or failed result;
- processed members whose step count or written member-output count is not
  `max_step - start_step + 1`;
- non-zero fatal data-quality counters for precipitation or active columns;
- wall-clock runtime above `--max-wall-s`, when configured;
- missing expected member output files, unless `--no-output-file-check` is used;
- failed requested probability-product generation, when
  `--write-probability-products` is used;
- exhausted transient FDB retries.

The CLI exits with `monitoring.json["recommended_exit_code"]`, so any critical
monitoring alert produces a non-zero process exit. Use `--monitoring-json` to
write an extra copy to a scheduler-specific location. The Python logger
`precip_type_diag.operational` emits run start, discovery, retries, per-step
progress, member failure, member completion, probability generation, and run
completion records. Use `--log-format json` for machine ingestion and route the
logs plus monitoring JSON into the normal batch scheduler or monitoring system.

FDB retries are deliberately narrow: they cover transient `fdb-utils list`, FDB
field retrieval, and field materialization/decode failures. Deterministic
science or validation failures, incomplete FDB contents after successful
listing, invalid shapes, invalid category codes, and strict probability
completeness failures are not retried.

## Rollback

Rollback means rerunning the previous accepted Git tag with its recorded
dependency/uenv environment and replacing the candidate output tree atomically at
the product publication boundary. Keep previous release tags and operational
records available until the new release has completed the agreed retention
period.
