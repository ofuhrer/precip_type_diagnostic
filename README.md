# precip_type_diag

`precip_type_diag` is the Balfrin/FDB precipitation-type diagnostic for
`ICON-CH1-EPS`, `ICON-CH2-EPS`, and deterministic `ICON-REA-L-CH1`.

The production contract is deliberately small:

- realtime EPS runs progressively process newly ingested forecast hours for all
  members and publish member NetCDF plus strict ensemble probabilities;
- REA-L-CH1 backfills process independent daily 00 UTC cycles, concatenate the
  24 validated records per day in chronological order, and atomically publish
  one categorical multi-message GRIB2 archive per month;
- `firdewsa` is the production default. The optional `icon` mode is an offline
  adaptation whose archived-input limitations are recorded in every summary.

The tool is FDB-only. File input, plotting, station postprocessing, and bias
correction are out of scope.

## Balfrin: setup once

```bash
ssh balfrin
cd /users/$USER/work
git clone git@github.com:ofuhrer/precip_type_diagnostic.git
cd precip_type_diagnostic
tools/setup_balfrin.sh
tools/run_balfrin.sh --help
```

`setup_balfrin.sh` validates `/usr/bin/uenv`, uses the reviewed
`fdb/5.21:v1` image, creates `.venv-fdb-5.21`, installs the pinned Numba
runtime, installs this repository, and verifies imports. The run wrapper always
selects the required FDB view and `PYTHONPATH`; no environment activation is
needed.

Override `PRECIP_TYPE_DIAG_UENV`, `PRECIP_TYPE_DIAG_FDB_IMAGE`,
`PRECIP_TYPE_DIAG_VENV`, or `PRECIP_TYPE_DIAG_FDB_SITE_PACKAGES` only when
validating a replacement runtime.

## A. Realtime ICON-CH1/2-EPS

Call this command whenever new fields may have arrived:

```bash
tools/run_balfrin.sh realtime ICON-CH1-EPS /users/$USER/work/ptype-fdb
tools/run_balfrin.sh realtime ICON-CH2-EPS /users/$USER/work/ptype-fdb
```

It discovers the latest partial cycle, finds the latest contiguous hour with
all required fields for every member, and processes only the range not already
published. Repeating the command is safe. A cycle lock prevents concurrent
writers, `CYCLE.json` tracks progress, and existing probability hours survive a
later failed increment.

For a DEPL-selected cycle, the compatibility wrapper is:

```bash
tools/run_depl_cycle.sh ICON-CH2-EPS 20260804 18 /users/$USER/work/ptype-fdb
```

The production defaults are all members, 8 member workers, 2-hour chunks,
NetCDF diagnostics, ensemble probabilities, Firdewsa, and three bounded FDB
retries. The command returns non-zero for a critical increment. It does not
submit a job or choose a partition.

`--through-step N` deliberately limits one invocation to an already available
hour. This is useful for controlled catch-up and for verifying step-by-step
publication; omit it in normal production.

## B. REA-L-CH1 backfill

Plan an inclusive range of daily cycle dates. The planner checks FDB inventory,
groups the available dates into monthly tasks, writes an immutable manifest,
and generates a Slurm array script:

```bash
CAMPAIGN=/users/$USER/work/ptype-rea-2005-2025
STAGING_ROOT=$SCRATCH/ptype-rea-2005-2025
ARCHIVE_ROOT=/store_new/mch/msopr/$USER/ptype-rea-2005-2025
tools/run_balfrin.sh backfill-plan \
  --start-date 20050101 --end-date 20250831 \
  --algorithm icon \
  --output-root "$ARCHIVE_ROOT" \
  --staging-root "$STAGING_ROOT" \
  --manifest "$CAMPAIGN/manifest.json"
sbatch "$CAMPAIGN/manifest.sbatch"
```

Use a smaller date range for a subset. Missing FDB cycle dates fail planning by
default; `--allow-missing-dates` records and excludes them explicitly. The
generated script uses `pp-long`, one month per array task, and a default
concurrency of 8. Every task still processes its dates one daily `0000` cycle at
a time under `STAGING_ROOT`; only the validated monthly archive is published to
`ARCHIVE_ROOT`. Staging and output roots are required not to overlap.

The measured categorical output projects to about `416 GB` (`0.38 TiB`) for
`20050101..20250831`. The 7,548 daily cycles become 248 monthly GRIB2 archives
instead of 181,152 single-message files. Only 249 files land in the archive
root (248 months plus `ARCHIVE_CONTRACT.json`); 747 receipts, locks, logs, and
control files remain under the campaign root, for about 996 persistent files in
total. A corrected ICON-mode 31-day January task took 1 hour 20 minutes,
averaging 152 seconds per daily cycle. The default eight-way monthly array
therefore projects to roughly 40 hours of ideal wall time; budget two to three
days plus queue and retry margin. Reserve at least `0.5 TiB` plus the local
retention margin and confirm the current long-term mount before launching the
full campaign.

Check campaign state with:

```bash
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json"
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json" --verify-outputs
```

The status command exits non-zero while a month is pending or failed.
`--verify-outputs` scans every GRIB message and checks its source date, step, and
validity time against the manifest, then recomputes the receipt checksum.
Re-submit failed monthly array indices with the generated script. A task skips
only a complete validated monthly archive; otherwise it reruns that month.
Schema-v1 daily manifests belong to release `v0.3.0`; re-plan them with the
current version to use the inode-safe layout.

### REA accumulation and date semantics

REA-L-CH1 accumulated and averaged fields restart at each daily `0000` cycle.
The tool therefore treats every cycle independently:

- cycle `D`, step 1 represents the interval ending at `D 01 UTC`;
- cycle `D`, step 24 represents the interval ending at `D+1 00 UTC`;
- hourly values are adjacent differences within cycle `D` only;
- negative adjacent differences are nonphysical within a cycle and are clamped
  to zero, with counts retained in `summary.json`;
- no accumulation is ever differenced across two cycle dates.

Manifest dates are cycle dates, not valid dates. A campaign ending with cycle
`20250831` includes a final interval ending at `20250901 00 UTC`.
Concatenation changes no GRIB message bytes or metadata. Messages are ordered by
cycle date and then step `1..24`; tasks never append concurrently to the same
published month.

## Supported data

| Model | FDB view | Members | Diagnostic steps | Cycle selection |
| --- | --- | ---: | ---: | --- |
| `ICON-CH1-EPS` | `realtime` | `000..010` | `1..33` or `1..45` | latest ingesting or explicit |
| `ICON-CH2-EPS` | `realtime` | `000..020` | `1..120` | latest ingesting or explicit |
| `ICON-REA-L-CH1` | `rea-l-ch1` | `000` | `1..24` | explicit daily cycle, `0000` |

The CLI enforces these horizons. Under the reviewed schedule, CH1 `0300` cycles
use 45 hours and the other cycles use 33; the cycle-specific horizon is recorded
in `CYCLE.json`. Realtime accumulated fields start at the forecast cycle; REA
accumulations start at the daily 00 UTC cycle. Neither contract permits an
accumulator reset inside a cycle. For every model and algorithm, negative
adjacent differences are therefore clamped to zero rather than replaced by the
current accumulated value. ICON rain, snow, and graupel component differences
use the same rule.

## Outputs and restart contract

```text
<realtime-output>/<MODEL>/<YYYYMMDD>/<HHMM>/
├── CONTRACT.json
├── CYCLE.json
├── increments/*.summary.json
├── increments/*.monitoring.json
├── <member>/lfffDDHHMMSS.ptype.nc
├── probabilities/*.ptype_prob.nc
├── summary.json
├── monitoring.json
└── DONE.json or FAILED.json

<rea-archive-root>/ICON-REA-L-CH1/
├── ARCHIVE_CONTRACT.json
└── <YYYY>/ptype_ICON-REA-L-CH1_<YYYYMM>.grib2

<campaign-root>/
├── manifest.json
├── manifest.sbatch
├── campaign-status.json
├── receipts/<index>-<YYYYMM>.json
├── locks/<index>-<YYYYMM>.lock
└── logs/<job>_<index>.out
```

`CONTRACT.json` prevents mixing algorithms, formats, masks, vertical cutoffs, or
probability modes in one realtime cycle directory. `ARCHIVE_CONTRACT.json`
binds a REA archive root to one algorithm and the manifest's exact cycle-date
set, preventing a partial-month campaign from replacing another campaign.
`RUNNING.json` exists during an increment and is atomically replaced by
`DONE.json` or `FAILED.json`. For progressive realtime, `CYCLE.json` is the
full-cycle authority: `status=ingesting` is healthy before the model horizon,
`status=complete` means the cycle-specific 33/45 or 120 hours are published, and `status=critical`
means the last increment needs attention.

Probability products are strict across every model member and use percent
values (`0..100`). Member NetCDF contains `ptype` and the diagnostic variables
needed for aggregation. Each REA monthly archive is an ordinary GRIB2 stream:
ecCodes and other GRIB tools read its messages sequentially, while every record
preserves the source grid/run template and packaged MeteoSwiss `PTYPE`
definition.

## Ad-hoc cycle and CLI

Use the unified wrapper for a deliberate fixed-cycle run:

```bash
tools/run_balfrin.sh cycle ICON-CH1-EPS 20260804 18 /users/$USER/work/ptype-fixed
tools/run_balfrin.sh cycle ICON-REA-L-CH1 20100101 00 /users/$USER/work/ptype-rea-one-day
```

The lower-level CLI remains available as `python -m precip_type_diag` or
`precip-type-diag`. Common diagnostic options include `--algorithm`,
`--members`, `--start-step`, `--max-step`, `--workers`, `--chunk-size`,
`--output-format`, `--write-probability-products`, `--no-resume`, and
`--lock-timeout-s`. Run each module with `--help` for its complete reference.

## Monitoring and troubleshooting

For realtime, start with `monitoring.json`, then `summary.json["failed"]` and the
run log. For REA, start with `campaign-status.json`, the monthly receipt, and its
Slurm log.
Deterministic science, validation, completeness, and output errors fail visibly;
only transient FDB list, retrieve, and decode failures are retried.
`data_quality.clamped_negative_total_precip_deltas` and
`data_quality.clamped_negative_icon_microphysics_deltas` quantify nonphysical
negative adjacent differences. Small nonzero counts can result from independent
GRIB packing at adjacent steps; large or changing counts require an upstream
accumulation review.

- `KeyError: 'activate'` from `uenv`: use `/usr/bin/uenv` version 8 or newer.
- FDB errors: confirm the `realtime` or `rea-l-ch1` view selected by the wrapper.
- missing FDB/Earthkit imports: rerun `tools/setup_balfrin.sh` and do not remove
  the FDB site-packages prefix.
- ecCodes cannot resolve `PTYPE`: use the FDB uenv or set
  `PRECIP_TYPE_DIAG_COSMO_DEFS` to the reviewed MeteoSwiss definitions.
- locked cycle: another writer owns `.cycle.lock` or `.progressive.lock`; wait
  for it or investigate the recorded lock owner before retrying.
- locked REA month: another array task owns the corresponding campaign lock;
  wait for it rather than appending or deleting the lock file.

## Local development

Python 3.11 or newer is required:

```bash
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

These checks use synthetic data and mocked FDB orchestration. Real FDB tests
run as scheduled jobs on Balfrin.

## More documentation

- [Science and architecture](docs/science-and-architecture.md)
- [Release and operations](docs/release-and-operations.md)
- [Release checklist](docs/release-checklist.md)
- [v0.4.0 monthly archive acceptance evidence](docs/acceptance/2026-08-05-v0.4.0-monthly-archive.md)
- [v0.3.0 release acceptance evidence](docs/acceptance/2026-08-04-v0.3.0-candidate.md)
- [Provenance and licensing](docs/provenance.md)
