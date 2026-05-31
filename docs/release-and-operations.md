# Release and Operations

This project is not production-accepted solely because tests pass. A release
must be tied to a code revision, dependency environment, scientific validation
record, and rollback plan.

## Pre-Release Gate

Before tagging a release:

1. Run local checks:

   ```bash
   python -m py_compile src/precip_type_diag/*.py test/*.py
   PYTHONPATH=src python -m pytest -q
   PYTHONPATH=src python -m precip_type_diag.benchmark
   ```

2. Confirm the GitHub Actions `tests` workflow passes for the release branch.
3. Run the scientific validation manifests described in
   [scientific-validation.md](scientific-validation.md).
4. Run a Balfrin FDB smoke test for each operational model:

   ```bash
   uenv run --view=realtime fdb/5.18:v3 -- \
     env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
     .venv-fdb/bin/python -m precip_type_diag \
     --model ICON-CH2-EPS \
     --members 000 \
     --max-step 1 \
     --output-root /users/$USER/work/ptype-fdb-smoke
   ```

5. Re-read at least one smoke-test output GRIB and verify `PTYPE` metadata and
   allowed category codes.
6. Archive the command output, `summary.json`, validation results, and data owner
   approval with the release decision.

## Versioning

Use Git tags for released code. The package version in `pyproject.toml` must be
updated for any release candidate or accepted production release. The operational
summary records:

- Python implementation and version;
- operating system summary;
- package versions for the runtime dependencies;
- Git commit, branch, and dirty-worktree flag when available;
- command-line arguments.

Do not promote output generated from a dirty worktree unless the exact diff is
archived and approved.

## Deployment

The production path is the module or console entry point:

```bash
python -m precip_type_diag ...
precip-type-diag ...
```

Run inside the documented realtime FDB `uenv` and keep the `uenv` image version
with the release record. If the FDB image changes, rerun smoke and scientific
validation before promotion.

## Monitoring

Operational supervision should alert on:

- non-zero process exit;
- non-empty `summary.json["failed"]`;
- missing expected member or step counts;
- non-zero active-column data-quality failures;
- wall-clock runtime outside the accepted production window;
- missing output GRIBs or missing `summary.json`.

The Python logger `precip_type_diag.operational` emits run start, discovery,
per-step progress, member failure, member completion, and run completion records.
Operations should route these logs into the normal batch scheduler or monitoring
system.

## Rollback

Rollback means rerunning the previous accepted Git tag with its recorded
dependency/uenv environment and replacing the candidate output tree atomically at
the product publication boundary. Keep previous release tags and validation
artifacts available until the new release has completed the agreed retention
period.
