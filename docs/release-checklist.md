# Release Checklist

Copy this template into the release record for every candidate. Commands and
interpretation are in the [README](../README.md) and
[release guide](release-and-operations.md).

## Candidate

- Version / proposed tag:
- Commit:
- Clean worktree: yes / no (attach approved diff if no)
- Python version:
- FDB image and `uenv` version:
- Tested views: `realtime` / `rea-l-ch1`
- ecCodes definition source:
- Release owner:
- Scientific approver:
- Operational approver:

## Automated gates

```bash
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
git diff --check
```

- Local gate result:
- GitHub Actions `tests` result:
- Wheel built and contents/entry points checked:

For ICON science changes:

```bash
PYTHONPATH=src python tools/verify_icon_fortran.py --icon-repo /path/to/icon-nwp
```

- ICON checkout commit:
- Fortran source SHA-256:
- Parity result:

## Balfrin acceptance evidence

Run every applicable row from the candidate revision as a CPU job. Record the
exact command, Slurm job ID/log, summary, monitoring file, and inspection.

| Path | Algorithm | Required behavior | Evidence |
| --- | --- | --- | --- |
| CH1 realtime | Firdewsa | bounded step 1, then step 2; prior probability retained | |
| CH1 long cycle | Firdewsa | `0300` cycle records a 45-hour horizon | |
| CH2 realtime | Firdewsa | bounded step 1, then step 2; prior probability retained | |
| REA monthly | Firdewsa | daily 1..24 semantics, ordered multi-message archive, failed task followed by atomic restart | |
| REA early era | Firdewsa | inventory/retrieval/output inspection | |
| REA middle era | Firdewsa | inventory/retrieval/output inspection | |
| REA late era | Firdewsa | inventory/retrieval/output inspection | |
| CH1/CH2/REA | ICON when required | archived microphysics and fidelity record | |
| REA analysis | ICON archive | exact four-bit repack, valid-time counts, Parquet/NetCDF reduction, restart | |
| REA regional analysis | compact ICON archive | reviewed mask, canonical checksum/count parity, regional Parquet/events, restart | |

For each executed row require:

- zero process exit and successful monitoring;
- correct model, view, cycle, algorithm, members, and step range;
- no lingering `RUNNING.json`; expected terminal/state marker exists;
- output metadata, shape, finite allowed categories, and probability scale are
  correct;
- restart evidence shows no mixed contract and no loss of an earlier product.

For REA confirm `time=0000`, cycle `D` steps `1..24`, and final valid time
`D+1 00 UTC`; confirm one task owns the month, the archive has exactly 24
ordered messages per selected cycle, and no daily staging files remain in the
archive root. For ICON mode confirm all three archived grid-scale microphysics
accumulations passed completeness checks and the summary reports unavailable
online convective/hail components. For all three models inspect the recorded
negative-delta clamp counts and confirm that no within-cycle decrease is
converted to the full current accumulator.

## Backfill campaign readiness

- Inventory manifest date range and missing-date policy reviewed:
- Depth-2 sentinel inventory, yearly checkpoint/resume, and full-range planner runtime reviewed:
- Daily task retained as the authoritative exact step/level completeness gate:
- Monthly grouping, staging root, archive root, and schema version reviewed:
- Generated Slurm partition, wall time, monthly array size, and concurrency reviewed:
- One monthly receipt, checksum, failed attempt, and restart reviewed:
- `backfill-status --verify-outputs` result:
- Storage, file-count estimate, filesystem quota, and retention owner:

## Archive-analysis readiness

- Completed immutable source manifest/root reviewed:
- Separate staging and analysis output roots reviewed:
- One-month source and four-bit decoded SHA-256 equality:
- Four-bit non-constant fields, zero-bit constant fields, size, and category range inspected:
- Step-24 following-month contribution verified:
- Hourly Parquet grain and UTC continuity verified:
- Monthly/seasonal/annual/full-period NetCDF counts verified:
- Freezing-rain codes 3/13 map and conditional denominator verified:
- Event definition and cell-area limitation reviewed:
- Failed/partial monthly task and restart reviewed:
- Reducer restart and `analysis-status --verify-outputs` result:
- If compact promotion/source retirement is in scope: full-range equality,
  exact source inventory, dual promotion receipts, exact-path deletion, dual
  retirement receipts, and post-retirement `analysis-status --verify-outputs`:
- Full campaign storage, file count, partition, concurrency, and retention owner:

## Regional-analysis readiness

- Boundary source/version, GeoJSON SHA-256, and feature selector reviewed:
- Mask grid SHA-256/UUID and selected-cell count reviewed:
- One compact month matches sealed byte/decoded checksums and all domain-hour Parquet evidence:
- Every regional hourly row partitions exactly across allowed categories:
- UTC continuity across monthly boundaries verified:
- Codes 3/13, code 12, and codes 3/12/13 catalogue definitions and event maxima verified:
- Cell-count/fraction limitation and downstream severity filters reviewed:
- Failed/partial task restart and `regional-status --verify-outputs` result:

## Decision and rollback

- Release decision and date:
- Accepted tag:
- Previous accepted tag:
- Previous runtime record:
- Product publication boundary:
- Rollback command/location:

Tag only after scientific and operational acceptance:

```bash
git tag -a vX.Y.Z -m "precip_type_diag vX.Y.Z"
git push origin vX.Y.Z
```
