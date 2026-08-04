# precip_type_diag

`precip_type_diag` is the Balfrin/FDB precipitation-type diagnostic for
`ICON-CH1-EPS`, `ICON-CH2-EPS`, and deterministic `ICON-REA-L-CH1`.

The production contract is deliberately small:

- realtime EPS runs progressively process newly ingested forecast hours for all
  members and publish member NetCDF plus strict ensemble probabilities;
- REA-L-CH1 backfills process independent daily 00 UTC cycles and publish
  categorical GRIB2;
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
writes an immutable manifest, and generates a Slurm array script:

```bash
CAMPAIGN=/users/$USER/work/ptype-rea-2005-2025
tools/run_balfrin.sh backfill-plan \
  --start-date 20050101 --end-date 20250831 \
  --output-root "$CAMPAIGN/output" \
  --manifest "$CAMPAIGN/manifest.json"
sbatch "$CAMPAIGN/manifest.sbatch"
```

Use a smaller date range for a subset. Missing FDB cycle dates fail planning by
default; `--allow-missing-dates` records and excludes them explicitly. The
generated script uses `pp-long`, one daily cycle per array task, and a default
concurrency of 8. Change planning options such as `--concurrency`,
`--partition`, or `--wall-time` before submission when operations require it.

Check campaign state with:

```bash
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json"
tools/run_balfrin.sh backfill-status "$CAMPAIGN/manifest.json" --verify-outputs
```

The status command exits non-zero while work is pending or failed. Re-submit
failed array indices with the generated script. A task skips only a day with a
successful marker and all 24 readable outputs; partial or failed days are safe
to run again.

### REA accumulation and date semantics

REA-L-CH1 accumulated and averaged fields restart at each daily `0000` cycle.
The tool therefore treats every cycle independently:

- cycle `D`, step 1 represents the interval ending at `D 01 UTC`;
- cycle `D`, step 24 represents the interval ending at `D+1 00 UTC`;
- hourly values are adjacent differences within cycle `D` only;
- no accumulation is ever differenced across two cycle dates.

Manifest dates are cycle dates, not valid dates. A campaign ending with cycle
`20250831` includes a final interval ending at `20250901 00 UTC`.

## Supported data

| Model | FDB view | Members | Diagnostic steps | Cycle selection |
| --- | --- | ---: | ---: | --- |
| `ICON-CH1-EPS` | `realtime` | `000..010` | `1..33` or `1..45` | latest ingesting or explicit |
| `ICON-CH2-EPS` | `realtime` | `000..020` | `1..120` | latest ingesting or explicit |
| `ICON-REA-L-CH1` | `rea-l-ch1` | `000` | `1..24` | explicit daily cycle, `0000` |

The CLI enforces these horizons. Under the reviewed schedule, CH1 `0300` cycles
use 45 hours and the other cycles use 33; the cycle-specific horizon is recorded
in `CYCLE.json`. Realtime accumulated fields start at the forecast cycle; REA
accumulations start at the daily 00 UTC cycle.

## Outputs and restart contract

```text
<output-root>/<MODEL>/<YYYYMMDD>/<HHMM>/
├── CONTRACT.json
├── CYCLE.json                         # progressive realtime authority
├── increments/*.summary.json          # progressive increment evidence
├── increments/*.monitoring.json
├── <member>/lfffDDHHMMSS.ptype.nc     # realtime
├── <member>/lfffDDHHMMSS.ptype.grib2  # REA
├── probabilities/*.ptype_prob.nc      # realtime
├── summary.json
├── monitoring.json
└── DONE.json or FAILED.json
```

`CONTRACT.json` prevents mixing algorithms, formats, masks, vertical cutoffs, or
probability modes in one cycle directory.
`RUNNING.json` exists during an increment and is atomically replaced by
`DONE.json` or `FAILED.json`. For progressive realtime, `CYCLE.json` is the
full-cycle authority: `status=ingesting` is healthy before the model horizon,
`status=complete` means the cycle-specific 33/45 or 120 hours are published, and `status=critical`
means the last increment needs attention.

Probability products are strict across every model member and use percent
values (`0..100`). Member NetCDF contains `ptype` and the diagnostic variables
needed for aggregation. REA GRIB2 preserves the source grid/run template and
encodes the packaged MeteoSwiss `PTYPE` definition.

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

Start with `monitoring.json`, then `summary.json["failed"]` and the run log.
Deterministic science, validation, completeness, and output errors fail visibly;
only transient FDB list, retrieve, and decode failures are retried.

- `KeyError: 'activate'` from `uenv`: use `/usr/bin/uenv` version 8 or newer.
- FDB errors: confirm the `realtime` or `rea-l-ch1` view selected by the wrapper.
- missing FDB/Earthkit imports: rerun `tools/setup_balfrin.sh` and do not remove
  the FDB site-packages prefix.
- ecCodes cannot resolve `PTYPE`: use the FDB uenv or set
  `PRECIP_TYPE_DIAG_COSMO_DEFS` to the reviewed MeteoSwiss definitions.
- locked cycle: another writer owns `.cycle.lock` or `.progressive.lock`; wait
  for it or investigate the recorded lock owner before retrying.

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
- [v0.3.0 candidate acceptance evidence](docs/acceptance/2026-08-04-v0.3.0-candidate.md)
- [Provenance and licensing](docs/provenance.md)
