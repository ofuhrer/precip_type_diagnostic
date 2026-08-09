# Swiss regional PTYPE-analysis acceptance

Date: 2026-08-09

## Candidate and scope

- accepted processing revision: `c0880bac80d30e269292c62fa021cb98baa4a6a8`
- review branch: `agent/regional-ptype-analysis`
- Balfrin image: `fdb/5.21:v1`
- canonical analysis manifest:
  `/users/olifu/work/ptype-rea-2005-2025-analysis/manifest.json`
- canonical compact archive:
  `/store_new/mch/msopr/olifu/ptype-rea-2005-2025-analysis/compact`
- regional campaign:
  `/users/olifu/work/ptype-rea-2005-2025-switzerland`
- regional products:
  `/store_new/mch/msopr/olifu/ptype-rea-2005-2025-switzerland`

The candidate adds a downstream, read-only regional pass over the canonical
compact archive. It does not rewrite the GRIB archive. Each monthly task
independently verifies canonical byte and decoded-value receipts, exact
four-bit/constant-field packing, every full-domain hourly decoded checksum,
and every full-domain category count before it writes regional hourly counts.

## Validation gates

The final local gate passed Python compilation, Ruff, mypy over 19 source files,
152 tests with 79.62% total coverage, the Numba benchmark, wheel build/content
inspection, and `git diff --check`. The workstation-wide `pip check` continues
to report the pre-existing globally installed `precip-type-diag 0.1.0` and
`fckitlib` dependency conflicts; these packages are outside the candidate
environment. A clean Balfrin setup from the exact candidate revision passed
`pip check` with no broken requirements before the real-data tests.

## Reviewed region mask

The mask uses the Natural Earth 1:10m admin-0 v5.1.1 GeoJSON release, selected
uniquely with `ADM0_A3=CHE`. The implementation applies longitude/latitude
point-in-polygon ray casting to ICON cell centres and excludes polygon holes.

- boundary SHA-256:
  `239eec57ac17f100a11e2536cffc56752c318b50ae765b0918ff7aab4ce8f255`
- grid SHA-256:
  `6a66098a512eb93c33e08e019b3f6f748c08dfaea5236727f2d637b7abd91e17`
- grid UUID: `17643da2574959b644d254a3cd6e2bc0`
- grid points: 1,147,980
- selected Swiss cell centres: 40,068
- immutable mask SHA-256:
  `f93848135dcbc6c619e4eea54d53c11ef4391f5927a640a6dc882503f110d2b5`

The 40,068 selected cells exactly reproduce the independently constructed mask
used by the preceding Swiss spatial analysis. The output reports counts and
fractions, not physical area, because no reviewed ICON cell-area dataset has
been supplied.

## One-month acceptance and restart

January 2010 was processed from a disjoint one-period campaign. Slurm job
`5043865` completed 744 messages in 10.59 seconds.

- source compact GRIB: 427,196,616 bytes
- source byte SHA-256:
  `c3e97250ee42be4ae04f46b2bf900e355aaf325cd49368e1d09f0fdde659e33a`
- source decoded SHA-256:
  `889ad3381aab58beac62b0efcb062a99adaf040ab43294be740a60c68ca814a2`
- regional hourly Parquet: 74,809 bytes
- regional decoded SHA-256:
  `57bdf3eab4e53a50a8855dc4efda0672109d852fcf1ce6ced80e16c067a91340`
- hourly coverage: 2010-01-01 01 UTC through 2010-02-01 00 UTC
- gaps, duplicates, checksum mismatches, and category-partition failures: 0

A deliberate restart in job `5043868` left the completed receipt byte-identical
at SHA-256
`7ce191faa6b5fb055d8b2b309e63e659f4841b0f8fff53e7d6520a2fd6ff0a47`.
Reducer job `5043869` and verified-status job `5043873` passed.

## Full campaign

The 248-period monthly array was Slurm `5043879`; the dependent reducer was
`5043880`.

- monthly tasks: 248 complete, 0 failed
- array wall interval: 2026-08-09 02:23:16–02:33:25 CEST (10 minutes 9 seconds)
- observed task elapsed times: 11–27 seconds
- reducer: 24 seconds, exit `0:0`
- records: 181,152 unique, consecutive valid UTC hours
- coverage: 2005-01-01 01 UTC–2025-09-01 00 UTC
- regional category partitions, temporal gaps/duplicates, and canonical
  checksum mismatches: 0
- exact canonical domain-count and message-checksum parity: pass
- compact packing validation: pass

The deliberate campaign-wide `regional-status --verify-outputs` job `5044158`
re-read and re-hashed all 102.2 GB of canonical compact GRIB and all regional
outputs. It completed in 2 minutes 54 seconds with exit code zero. Final status
reported 248 of 248 periods complete, no failed or pending periods, reduction
complete, and `verified_outputs: true`.

The permanent regional output tree contains 256 files and 34,334,856 bytes.
The campaign tree contains 751 files and 14,988,149 bytes; its pinned Natural
Earth GeoJSON is the largest component. The combined footprint is about 49.3
MB, of which only 34.3 MB is on `/store_new`.

## Products

- `ptype_hourly_counts_switzerland.parquet`: 16,312,232 bytes, SHA-256
  `a50d0f6af8d3aacd7df500921b1db9c1ecb3d2088f489c61ab3f8ab2dcff57ae`
- `high_impact_events_switzerland.parquet`: 147,183 bytes, SHA-256
  `9fb304136881ff7f801b6b78d052d38566764e6a53b8e8c0928ebcc144db7863`
- `freezing_drizzle_events_switzerland.parquet`: 341,917 bytes, SHA-256
  `19724c3c9324d7a3f486d705d95b7f66388524fac668945b0366d082da873e02`
- `icy_liquid_events_switzerland.parquet`: 346,666 bytes, SHA-256
  `96fa2695430c9f41414a6b46d58bde23bb22176eab23bf8d27d09833e9a8ff47`
- `REGIONAL_DATA_QUALITY_REPORT.json`: status `pass`, SHA-256
  `50c47a28b39e64019b94de4434e6ec013b154f3062f8bdf6c7fdbaaf1b4a9cb8`

Independent local reconstruction checked non-null values, exact row and time
coverage, unique hours, constant region size, all hourly category and event
identities, report totals, event IDs, duration/bounds, and every event catalogue
against the regional hourly table. All 24 checks passed.

## Descriptive findings and interpretation

Across 7,258,398,336 Swiss cell-hours, 1,537,138,701 are precipitating. Codes
3 and 13 occupy 1,050,698 cell-hours: 0.01448% of all cell-hours and 0.06835% of
precipitating cell-hours. Code 12 occupies 9,348,019 cell-hours: 0.12879% of all
cell-hours and 0.60814% of precipitating cell-hours. Freezing-drizzle cell-hours
are therefore 8.90 times the combined freezing-rain cell-hours in this offline
diagnostic.

The catalogues are deliberately permissive temporal screens: an event remains
active while at least one selected cell has a matching category. High-impact
codes 3/13 produce 4,066 runs, of which 93 peak over at least 1% of Swiss cells
and 27 peak over at least 5%. Code 12 produces 9,044 runs, of which 961 and 58
cross the same thresholds. Combined codes 3/12/13 produce 9,166 runs, with 996
over 1% and 87 over 5%.

Any-cell occurrence is strongly seasonal but must not be interpreted as broad
regional impact: combined icy-liquid precipitation occurs somewhere among the
40,068 selected centres in 52.01% of DJF hours, 52.51% of MAM hours, 23.21% of
JJA hours, and 40.92% of SON hours. Median catalogue duration is two hours for
all three families; many events affect only a handful of cells.

The daily-accumulation boundary check gives a step-1/other-hour precipitation
ratio of 0.985. Category-specific ratios are 1.346 for rare codes 3/13, 1.078
for code 12, and 1.105 for combined icy liquid. The precipitation total is
smooth across the boundary. The category ratios mix real diurnal climatology
and sampling noise and are supporting fingerprints, not standalone proof of
reset correctness; that proof remains the upstream adjacent-difference
contract and the exact canonical parity checks.

These frequencies describe the offline ICON-style diagnostic, not observations
or verified hazard occurrence. The archived REA input lacks convective and hail
rates used by the online diagnostic. No spatial object catalogue is claimed:
connected-event analysis still requires reviewed grid-neighbour and cell-area
datasets.

## Decision

Accepted as a reproducible, resumable downstream regional-analysis workflow.
The Swiss hourly counts and all three event catalogues are suitable for temporal
screening and thresholded event selection without reopening GRIB. They do not
replace the canonical compact archive or the gridded frequency products.
