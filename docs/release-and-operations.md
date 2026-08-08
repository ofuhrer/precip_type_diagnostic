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
tools/submit_backfill_campaign.sh \
  --start-date 20050101 --end-date 20250831 \
  --algorithm icon \
  --output-root "$ARCHIVE_ROOT" \
  --staging-root "$STAGING_ROOT" \
  --manifest "$CAMPAIGN/manifest.json"
```

The schema-v2 planner uses bounded parallel depth-2 FDB index probes rather
than scanning all matching GRIB messages. It splits the range into years,
checks every required field at step 24 (`HHL` at step 0), and atomically writes
`manifest.inventory.json` after each completed year. Re-running the same
campaign resumes those years. The checkpoint is removed after successful
manifest and script publication. This is a date-availability prefilter; every
daily task still validates all required steps and levels before publication.
The accepted full-range restart completed in 13m52s after reusing four years;
budget 15–20 minutes for a clean plan under normal FDB load. The 59-minute
planner allocation leaves margin, and an interrupted attempt keeps its last
complete yearly checkpoint.
The submission wrapper runs this planner on `pp-short` and submits the generated
monthly `pp-long` array only if strict planning completes successfully.

The manifest groups exact inventory dates by calendar month. The generated
`pp-long` array assigns one month to each task. A task processes every selected
day independently at `0000` and steps `1..24` in bounded scratch,
concatenates the validated GRIB messages in cycle-date/step order, verifies the
complete stream, and atomically publishes one monthly file. Each task writes a
monthly receipt under `receipts/` and returns non-zero on critical monitoring.
The manifest is immutable; use a new campaign and output root to change dates,
algorithm, archive bounds, or missing-date policy. Schema-v1 daily manifests
remain tied to `v0.3.0` and must be re-planned for the monthly layout.

A corrected ICON-mode 31-day month produced 1,708,342,296 bytes and finished in
1 hour 19 minutes 54 seconds. Its daily science wall times averaged 152.4
seconds. The `20050101..20250831` calendar range therefore projects to about
416 GB (`0.38 TiB`) of categorical GRIB2 in 248 monthly archive files and about
40 hours of ideal wall time with eight concurrent monthly tasks. Including
receipts, locks, Slurm logs, the archive contract, manifest, script, and
campaign status, the planner projects 249 files in the archive root, 748 in the
campaign root, and 997 persistent files in total. Require at least `0.5 TiB`
plus the agreed retention margin and reserve two to three days plus queue and
retry margin. Recalculate from a representative month when packing, grid,
algorithm, or archive bounds change.

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

## REA archive-analysis scheduling

Run analysis against a completed source campaign and a new output root. The
source manifest SHA-256, source root, category contract, four-bit packing, and
valid-time semantics are immutable in `ANALYSIS_CONTRACT.json`. The standard
submission command creates a monthly `pp-long` array and submits the reducer
with `afterok:<array-job-id>`.

Each task owns one source month and publishes three outputs: compact GRIB,
valid-month grid counts, and hourly domain counts. It stages the GRIB on the
destination filesystem, independently rereads every compact message, compares
the canonical decoded checksum, calls `fsync`, and atomically renames it. A
complete receipt records source and compact byte checksums, decoded checksum,
message count, grid identity, category totals, valid-month contribution hours,
and compression ratio. Status reuses only size-consistent complete receipts;
`--verify-outputs` additionally recomputes all checksums and decodes the compact
GRIB stream.

The reducer requires every monthly receipt, merges the hour-grain Parquet files
in strictly contiguous valid-time order, combines month-boundary count
contributions, and atomically publishes the final Parquet/NetCDF products and
`REDUCTION.json`. Re-submit failed array indices; rerun the reducer after all
tasks are complete. Never point analysis output or staging inside the source
archive tree.

Acceptance requires a real month with all expected messages, exact decoded
source/compact equality, `bitsPerValue=4`, correct step-24 valid-month transfer,
restart reuse, a successful reducer, readable Parquet/NetCDF products, and a
passing data-quality report. Do not infer physical event area from cell count;
the source PTYPE message has no cell-area field.

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

Within-cycle accumulators must be monotonic, but independent GRIB packing can
produce small negative decoded differences. The diagnostic clamps those values
to zero uniformly for realtime and REA, including ICON's archived rain, snow,
and graupel components. It records the affected value counts as
`clamped_negative_total_precip_deltas` and
`clamped_negative_icon_microphysics_deltas` under `data_quality`. Review
unexpectedly large values or changes between cycles as an upstream data
incident; do not reinterpret a negative delta as a reset and substitute the
full current accumulation.

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
