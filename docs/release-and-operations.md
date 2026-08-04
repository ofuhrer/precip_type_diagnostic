# Release and Operations

Use [release-checklist.md](release-checklist.md) to record the exact revision,
runtime, validation evidence, approvals, and rollback target. Setup and smoke
commands are maintained in the [README](../README.md); source and licensing
constraints are in [provenance.md](provenance.md).

## Release Gate

Before tagging or promoting:

1. Run the complete local gate from the README in a clean worktree and confirm
   the GitHub Actions `tests` workflow passes.
2. If ICON science changed, run the pinned executable Fortran comparison:

   ```bash
   PYTHONPATH=src python tools/verify_icon_fortran.py --icon-repo /path/to/icon-nwp
   ```

3. If packaging changed, build a wheel and confirm the ecCodes definition files
   are included.
4. Run Balfrin smoke tests from the candidate revision for CH1, CH2, and one
   explicit REA-L-CH1 day. Test both algorithms for science or FDB changes.
5. Re-read at least one output per source and verify `PTYPE` metadata, shape,
   step, and allowed category codes. Require `monitoring.json["ok"] == true`.
6. Archive commands, logs, `summary.json`, `monitoring.json`, runtime versions,
   and scientific/operational approvals with the release decision.

Do not promote output from a dirty worktree unless its exact diff is archived
and approved.

## Versioning and Provenance

Update the package version in `pyproject.toml`, then create an annotated tag:

```bash
git tag -a vX.Y.Z -m "precip_type_diag vX.Y.Z"
git push origin vX.Y.Z
```

`summary.json` records Python/platform and dependency versions, Git revision and
dirty state, arguments, FDB source, algorithm, mask, and ICON fidelity limits.
Keep the tested uenv image and view with the release record.

## Deployment

Run the module or console entry point inside the matching FDB view:

```bash
python -m precip_type_diag ...
precip-type-diag ...
```

The reviewed Balfrin runtime is `/usr/bin/uenv` 8+ with `fdb/5.21:v1`, the FDB
site-packages first on `PYTHONPATH`, and the project venv described in the
README. Re-run live smokes whenever the image or environment changes.

For a scheduler-selected realtime cycle:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS 20260531 18 /users/$USER/work/ptype-fdb
```

The wrapper is limited to CH1/CH2 realtime forecasts. It selects all members,
8 workers, 2-hour chunks, NetCDF probabilities, JSON INFO logs, and three
bounded FDB retries. It does not submit a job or choose a partition. Invoke the
module directly in `--view=rea-l-ch1` for an explicit REA day.

For manual Balfrin validation, use the generally open `pp-short` partition when
the job fits its one-hour limit. Restricted production partitions are not the
default for development or release smokes. A minimal submission is:

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

## Monitoring

Each run directory contains:

- `summary.json`: selection, outputs, failures, data quality, timings, retries,
  FDB source, algorithm fidelity, and provenance;
- `monitoring.json`: scheduler-facing status and recommended exit code;
- `RUNNING.json`, atomically replaced by `DONE.json` or `FAILED.json`.

Monitoring is critical when a member is missing/failed/incomplete, fatal data
quality is non-zero, expected files are absent, requested probability generation
fails, FDB retries are exhausted, or the configured wall limit is exceeded. A
critical status produces a non-zero process exit.

Use `--monitoring-json` for an additional scheduler-facing copy and
`--log-format json` for machine-readable logs. Start incident review with
`monitoring.json`, then inspect its alerts, `summary.json["failed"]`, retry
counters, and the run log.

Retries are intentionally limited to transient FDB listing, retrieval, and
decode/materialization failures. Invalid data, shapes, categories, incomplete
FDB content, and strict probability failures are not retried.

## Rollback

Run the previous accepted tag in its recorded runtime, verify it with the same
smoke contract, and replace candidate products atomically at the publication
boundary. Retain the previous tag, runtime record, and outputs until the new
release completes its agreed retention period.
