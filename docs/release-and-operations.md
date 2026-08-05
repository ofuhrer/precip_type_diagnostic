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
   REA monthly archive containing representative daily cycles, including a
   failed/partial task followed by atomic restart, and representative
   early/middle/late REA inventory dates.
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
STAGING_ROOT=$SCRATCH/ptype-rea-campaign
ARCHIVE_ROOT=/store_new/mch/msopr/$USER/ptype-rea-campaign
tools/run_balfrin.sh backfill-plan \
  --start-date 20050101 --end-date 20250831 \
  --output-root "$ARCHIVE_ROOT" \
  --staging-root "$STAGING_ROOT" \
  --manifest "$CAMPAIGN/manifest.json"
sbatch "$CAMPAIGN/manifest.sbatch"
```

The schema-v2 manifest groups exact inventory dates by calendar month. The
generated `pp-long` array assigns one month to each task. A task processes every
selected day independently at `0000` and steps `1..24` in bounded scratch,
concatenates the validated GRIB messages in cycle-date/step order, verifies the
complete stream, and atomically publishes one monthly file. Each task writes a
monthly receipt under `receipts/` and returns non-zero on critical monitoring.
The manifest is immutable; use a new campaign and output root to change dates,
algorithm, archive bounds, or missing-date policy. Schema-v1 daily manifests
remain tied to `v0.3.0` and must be re-planned for the monthly layout.

A complete 31-day Firdewsa month produced 1,708,342,296 bytes and finished in
1 hour 10 minutes 57 seconds. Its daily science wall times averaged 135.8
seconds. The `20050101..20250831` calendar range therefore projects to about
416 GB (`0.38 TiB`) of categorical GRIB2 in 248 monthly archive files and about
36 hours of ideal wall time with eight concurrent monthly tasks. Including
receipts, locks, Slurm logs, the archive contract, manifest, script, and
campaign status, the planner projects 249 files in the archive root, 747 in the
campaign root, and 996 persistent files in total. Require at least `0.5 TiB`
plus the agreed retention margin and reserve about two days plus queue and retry
margin. Recalculate from a representative month when packing, grid, algorithm,
or archive bounds change.

Check progress cheaply, then verify all outputs before acceptance:

```bash
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json"
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json" --verify-outputs
```

Re-submit specific failed/pending monthly indices with Slurm's `--array`
override. A task skips only a complete archive whose receipt, size, message
count, dates, steps, validity metadata, and SHA-256 checksum match the manifest
and archive. Otherwise it reruns that month. The worst restart unit is one
month; each constituent day still starts from step 0 and never differences step
24 against another cycle.

## Concurrency and publication safety

One `.progressive.lock` serializes realtime orchestration and one `.cycle.lock`
serializes core publication for a model/cycle/output root. The lock file may
remain after a run because the kernel lock, not file existence, determines
ownership. Do not remove a lock file while a process may be active.

REA uses one campaign-root lock per monthly index. Staging and archive roots
must not overlap. A task writes a hidden partial file on the destination
filesystem, validates it, calls `fsync`, and publishes with an atomic rename.
Never have multiple tasks or shell commands append directly to the same monthly
target, even though concatenated GRIB messages are a valid GRIB2 stream.

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
realtime or `campaign-status.json` for REA, then inspect the monthly receipt and
Slurm log. Daily summaries and monitoring files are transient staging evidence;
the receipt retains their compact wall-time, data-quality, and retry summaries.

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
