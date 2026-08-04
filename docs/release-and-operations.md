# Release and Operations

The [README](../README.md) is the operator quick start. This guide defines the
scheduling, monitoring, restart, release, and rollback contracts. Record each
candidate in [release-checklist.md](release-checklist.md).

## Release gate

Before tagging or promotion:

1. Run the complete local gate in a clean worktree and confirm the GitHub
   Actions `tests` workflow passes.
2. Build and inspect a wheel after packaging changes, including the ecCodes
   definition overlay and all console entry points.
3. Run scheduled Balfrin acceptance jobs from the exact candidate revision:
   CH1 progressive steps 1 then 2 plus a 45-hour-cycle horizon check, CH2
   progressive steps 1 then 2, a complete
   REA day with a partial-run restart, and representative early/middle/late REA
   inventory dates.
4. Test both algorithms when science or FDB selection changed. ICON science
   changes also require the pinned executable Fortran comparison.
5. Inspect at least one member and probability product per realtime model and
   one GRIB2 product per REA era. Verify source, step, shape, category codes,
   probability scale, and `monitoring.json["ok"]`.
6. Archive commands, job IDs, logs, summaries, monitoring files, runtime
   versions, and the scientific and operational approvals.

Do not promote output from an unrecorded dirty worktree.

## Reviewed Balfrin runtime

The reviewed baseline is `/usr/bin/uenv` 8+, `fdb/5.21:v1`, and the matching
`realtime` or `rea-l-ch1` view. Install it once with:

```bash
tools/setup_balfrin.sh
```

The setup script pins the packages absent from the FDB image and verifies the
combined runtime. Re-run the full live gate when the image, view, Python ABI,
or pinned packages change.

## Realtime scheduling

The production action is idempotent:

```bash
tools/run_balfrin.sh realtime ICON-CH1-EPS /users/$USER/work/ptype-fdb
tools/run_balfrin.sh realtime ICON-CH2-EPS /users/$USER/work/ptype-fdb
```

A scheduler or DEPL event may call it repeatedly while ingestion is active.
For an event-selected cycle, use:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS YYYYMMDD HH /users/$USER/work/ptype-fdb
```

The wrapper processes every newly complete contiguous hour. It does not submit
a job or choose a partition. A manual Balfrin validation that fits within one
hour should use the generally open `pp-short` CPU partition:

```bash
#!/usr/bin/env bash
#SBATCH --job-name=ptype-eps
#SBATCH --partition=pp-short
#SBATCH --time=00:59:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16

set -euo pipefail
[ -f /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh
if [[ -n "${USER_ENV_ROOT:-}" ]]; then
  module use "$USER_ENV_ROOT/modules"
fi
cd /users/$USER/work/precip_type_diagnostic
tools/run_depl_cycle.sh "$MODEL" "$DATE" "$TIME" "$OUTPUT_ROOT"
```

`CYCLE.json` is the scheduler-facing full-cycle record:

- `ingesting`: all processed increments succeeded but the cycle-specific
  33/45-hour CH1 or 120-hour CH2 horizon
  has not yet been reached;
- `complete`: the full horizon is published;
- `critical`: the latest attempted increment failed monitoring and the command
  exited non-zero.

Each successful invocation with new data appends an immutable increment record.
An invocation with no new hour validates state, writes the refreshed state, and
does no FDB data retrieval. `--through-step` is reserved for controlled catch-up
and acceptance tests.

## REA backfill scheduling

Planning is inventory-backed and uses inclusive cycle dates:

```bash
CAMPAIGN=/users/$USER/work/ptype-rea-campaign
tools/run_balfrin.sh backfill-plan \
  --start-date 20050101 --end-date 20250831 \
  --output-root "$CAMPAIGN/output" \
  --manifest "$CAMPAIGN/manifest.json"
sbatch "$CAMPAIGN/manifest.sbatch"
```

The generated `pp-long` array assigns one 00 UTC cycle and steps 1..24 to each
task. Each task writes a receipt under `receipts/`, records its attempt in the
run summary, and returns non-zero on critical monitoring. The manifest is the
immutable campaign contract; create a new campaign directory to change dates,
algorithm, output root, or missing-date policy.

A verified 24-file Firdewsa day used about 55.1 MB and 157 seconds. The
`20050101..20250831` calendar range projects to about 416 GB (`0.38 TiB`) of
categorical GRIB2; require at least `0.5 TiB` plus the agreed retention margin
before submitting the full array. Recalculate from a representative day when
packing, grid, algorithm, or archive bounds change.

Check progress cheaply, then verify all outputs before acceptance:

```bash
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json"
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json" --verify-outputs
```

Re-submit specific failed/pending indices with Slurm's `--array` override. A
task skips only a verified complete day. Otherwise it reruns the complete daily
cycle; it never differences step 24 of one day against the next day's step 0.

## Concurrency and publication safety

One `.progressive.lock` serializes realtime orchestration and one `.cycle.lock`
serializes core publication for a model/cycle/output root. The lock file may
remain after a run because the kernel lock, not file existence, determines
ownership. Do not remove a lock file while a process may be active.

`CONTRACT.json` is immutable. A different algorithm, output format, mask,
vertical cutoff, or probability mode requires a different output root.
Verified complete member outputs are reused on retry;
invalid or incomplete ranges are regenerated. Probability publication stages a
complete directory and replaces it atomically, retaining prior hours.

## Monitoring and incidents

Each core run writes:

- `summary.json`: source, cycle, outputs, failures, data quality, timings,
  retries, resume counts, algorithm fidelity, and runtime/Git provenance;
- `monitoring.json`: alerts, status, and recommended exit code;
- `RUNNING.json`, atomically replaced by `DONE.json` or `FAILED.json`.

Monitoring is critical for failed or incomplete members, fatal data quality,
missing expected files, strict probability failure, exhausted FDB retries, or a
configured wall-time violation. Start incident review with `CYCLE.json` for
realtime or `campaign-status.json` for REA, then inspect the referenced
monitoring file, summary, receipt, and logs.

Retries are limited to transient FDB listing, retrieval, and decoding.
Scientific validation, shape, category, completeness, contract, and publication
errors remain visible. Do not bypass them with scheduler retry loops alone.

## Versioning, promotion, and rollback

`summary.json` records the Git revision and dirty state. For an accepted release,
update `pyproject.toml`, secure the approvals in the checklist, and create an
annotated tag:

```bash
git tag -a vX.Y.Z -m "precip_type_diag vX.Y.Z"
git push origin vX.Y.Z
```

Rollback by running the previous accepted tag in its recorded runtime and a new
output root, verifying the same smoke contract, then switching the downstream
publication boundary atomically. Retain both product roots and release records
through the agreed rollback window.
