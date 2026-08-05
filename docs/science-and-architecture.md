# Science and Architecture

For installation and run commands, start with the [README](../README.md). This
document defines the scientific and data contracts behind those commands.

## Diagnostic Modes

Both modes classify each active atmospheric column from its wet-bulb thermal
structure, melting/refreezing energy, surface conditions, and precipitation.

### Firdewsa (default)

`firdewsa` preserves Firdewsa Zukanovic's MSc implementation of the Modified
Bourgouin method. `profile.py` is the scalar reference;
`numba_backend.py` is the behaviorally matched production path.

### ICON-like

`icon` follows ICON commit
[`50da7c5924994f7626688eb5185b8e66c781b12e`](https://gitlab.dkrz.de/icon/icon-nwp/-/commit/50da7c5924994f7626688eb5185b8e66c781b12e).
It uses ICON thermodynamic formulas, interface-height layer depths,
zero-crossing energy integration, ICON thresholds, an interval-scaled trace
mask, packing-safe accumulation differencing, and archived surface
microphysics.
`icon_profile.py` is the scalar reference and `icon_numba_backend.py` is the
accelerated path. Frozen vectors and the executable Fortran harness pin parity
to the cited commit.

The offline inputs omit convective rain, convective snow, and hail rates used by
the online ICON call. The tool does not infer them: `summary.json` records the
available components and reports that exact online refinement parity is false.

## FDB Contracts

| CLI model | View | FDB identity (`class/stream/expver/model`) | Contract |
| --- | --- | --- | --- |
| `ICON-CH1-EPS` | `realtime` | `od/enfo/0001/icon-ch1-eps` | members `000..010`, steps `0..33` or `0..45`; latest ingesting or complete cycle |
| `ICON-CH2-EPS` | `realtime` | `od/enfo/0001/icon-ch2-eps` | members `000..020`, steps `0..120`; latest ingesting or complete cycle |
| `ICON-REA-L-CH1` | `rea-l-ch1` | `rd/reanl/r001/icon-rea-l-ch1` | member `000`, daily `0000` cycle, steps `0..24`; date/time must be explicit |

The reviewed REA-L archive spans 2005–2025. Some surface variables mix 10-minute and
hourly steps, so completeness checks constrain the requested hourly range.
`fdb-utils list` omits `timespan` for this archive; retrieval still uses
`timespan=none` for instantaneous fields and `timespan=fs` for accumulations.

The reviewed realtime inventory exposes CH1/CH2 EPS, not KENDA-CH1; KENDA is
therefore not a supported model.

Under the reviewed operational schedule, CH1 `0300` cycles use the 45-hour
all-member horizon and the other cycle times use 33 hours. The model-wide hard
cap remains 45, while progressive state and default explicit-cycle processing
select the expected horizon from the cycle time. This avoids treating a normal
33-hour cycle as incomplete or truncating a long cycle.

## Input and Accumulation Contract

All models require:

| Field | `paramId` | Use |
| --- | ---: | --- |
| `T` | `500014` | full-level temperature |
| `P` | `500001` | full-level pressure |
| `QV` | `500035` | full-level specific humidity |
| `HHL` | `500008` | half-level height, retrieved at step 0 |
| `TOT_PREC` | `500041` | accumulated precipitation |
| `T_G` | `500010` | ground temperature |

ICON mode also requires these accumulated grid-scale fields:

| Field | `paramId` | Derived hourly-mean rate |
| --- | ---: | --- |
| `RAIN_GSP` | `500134` | rain |
| `SNOW_GSP` | `500053` | snow |
| `GRAU_GSP` | `500146` | graupel |

Hourly precipitation is `TOT_PREC(current) - TOT_PREC(previous)`. ICON surface
rates use the same adjacent-step difference divided by 3600 seconds. The
accumulation contract is monotonic within a forecast or daily reanalysis cycle,
but adjacent GRIB messages are packed independently and can decode to a small
negative difference. Negative differences are nonphysical and are clamped to
zero for `TOT_PREC`, `RAIN_GSP`, `SNOW_GSP`, and `GRAU_GSP`. They are never
replaced by the current accumulator: doing so would turn packing noise into a
spurious hourly amount. Clamp counts are preserved in `summary.json` for both
realtime and REA processing so an actual upstream contract violation remains
observable.

Realtime accumulations start at the model forecast cycle. REA-L accumulations
start at its daily 00 UTC cycle and end at step 24. Cycle `D`, step 24 is valid
at `D+1 00 UTC`; it still belongs to cycle `D`. The implementation enforces REA
`time=0000`, caps the run at step 24, and never crosses a daily boundary. The
backfill manifest therefore records cycle-date bounds and the corresponding
valid-time coverage separately. Monthly archive packing happens only after each
daily cycle has passed monitoring and output verification; it does not combine
or difference accumulations across cycle dates.

Production starts at step 1; step 0 only initializes the first delta. The CLI
accepts `--start-step 0` for debugging, but that output is not a physically
defined hourly diagnostic.

## Column and Grid Behavior

For each active column, the diagnostic:

1. derives dew point, wet-bulb temperature, and ice relative humidity;
2. identifies precipitation-generation and sublimating layers;
3. estimates ice probability and melting/refreezing energies;
4. converts probabilities and surface refinements to one category.

`grid.py` validates arrays, selects active columns, and dispatches to the chosen
Numba backend. Columns at or below the precipitation mask become `NO_PRECIP`
without thermodynamic diagnosis. Non-finite precipitation is always fatal;
non-finite thermodynamic inputs are fatal in active columns and counted only in
dry columns. ICON mode also requires positive temperature/pressure, descending
half-level interfaces, and finite surface rates in active columns.

The default 12 km `HHL` cutoff removes upper levels before diagnosis. It is a
reviewed performance/science contract, not a tuning parameter.

## Category Contract

| Code | Meaning |
| ---: | --- |
| `0` | no precipitation |
| `1` | rain |
| `3` | freezing rain |
| `5` | snow |
| `6` | wet snow |
| `7` | rain/snow mixture |
| `8` | ice pellets |
| `9` | graupel |
| `10` | hail |
| `12` | freezing drizzle |
| `13` | freezing rain on ground |

The package includes an ecCodes overlay for MeteoSwiss `PTYPE`. Output writers
validate shape, finite integer values, and the allowed code set.

## Production Flows

```text
FDB discovery/checks -> HHL selection -> chunk retrieval -> array validation
-> column diagnosis -> member output -> optional strict probability aggregation
-> summary + monitoring + final marker
```

- `operational.py` owns source selection, FDB checks/retries, prefetching,
  multiprocessing, immutable cycle contracts, locking, verified resume, output
  orchestration, and summaries.
- `realtime.py` owns progressive EPS ingestion. It advances only through the
  latest contiguous complete hour, runs all members, preserves earlier
  probability hours, and records full-cycle state in `CYCLE.json`.
- `backfill.py` owns immutable REA inventory manifests, monthly Slurm array
  tasks, bounded daily staging, chronological GRIB concatenation, atomic
  monthly publication, receipts, restart verification, and campaign status.
  Planning uses resumable year segments and depth-2 FDB index probes for
  required-field sentinel presence. It deliberately does not enumerate every
  GRIB record; the daily core remains authoritative for exact step/level
  completeness.
- `gribio.py` writes GRIB2 from the current `TOT_PREC` template, preserving grid
  and run metadata while replacing parameter metadata and values. It also
  iterates metadata from multi-message archives for publication verification.
- `netcdfio.py` writes member data; `probabilities.py` strictly aggregates every
  requested member. NetCDF currently has generic `cell` or `y/x` dimensions,
  without geospatial coordinates or a grid mapping.
- `monitoring.py` checks completeness, failures, data quality, expected files,
  probability generation, retries, and optional wall time.
- A cycle directory has one immutable `CONTRACT.json`, preventing accidental
  mixing of algorithm, format, mask, cutoff, or probability mode. POSIX locks
  prevent concurrent publishers.
- Core increments atomically publish `RUNNING.json`, then `DONE.json` or
  `FAILED.json`. `CYCLE.json` is authoritative for progressive full-cycle state.
- A REA array task owns exactly one month. It invokes the unchanged daily core
  for each selected `0000` cycle in scratch, concatenates the 24 verified GRIB2
  messages in date/step order, validates the complete stream, and atomically
  renames it into the archive root. Workers never append concurrently to a
  published archive.
- Retries cover transient FDB list, retrieve, and decode/materialization
  failures only; deterministic failures remain visible.

## Defaults

| Setting | Default |
| --- | --- |
| algorithm | `firdewsa` |
| first step | `1` |
| workers / chunk size | `8` / `2` hours |
| prefetch | enabled |
| low-level member output | GRIB2 |
| vertical cutoff | `12000 m` |
| Firdewsa / ICON mask | `0.0 mm` / `0.01 mm` per hour |
| probability scale | `0..100` percent |

Probability publication requires NetCDF and complete output from every requested
member. Its thresholded intensity uses a 30% probability threshold and a
`0.01 mm/h` precipitation mask.

The accepted wrappers intentionally narrow these generic defaults: realtime EPS
uses all members, NetCDF diagnostics, and probability products; REA backfills
use member `000`, steps `1..24`, and categorical GRIB2. Both use Firdewsa unless
an explicitly reviewed campaign selects `icon`. The reviewed REA workflow in
the README selects ICON mode explicitly to match the online diagnostic as
closely as the archived rate components allow.

## Verification

Automated tests cover scalar science, Numba parity, frozen ICON Fortran vectors,
grid validation, format contracts, probabilities, monitoring, CLI behavior, and
mocked FDB orchestration. There are no real GRIB fixtures; live FDB verification
uses scheduled Balfrin jobs and the acceptance matrix in the
[release checklist](release-checklist.md).

For an executable ICON comparison:

```bash
PYTHONPATH=src python tools/verify_icon_fortran.py --icon-repo /path/to/icon-nwp
```

The verifier extracts the pinned `mo_diag_precip_type.f90`, compiles it with
minimal stubs, and compares deterministic columns with Python. Only use
`--write-vectors test/data/icon_fortran_reference.json` when intentionally
reviewing and updating the reference contract.

## References

- Firdewsa Zukanovic, *Precipitation Type Diagnostic for ICON* (MSc thesis).
- Bourgouin (2000), [*A Method to Determine Precipitation Types*](https://doi.org/10.1175/1520-0434%282000%29015%3C0583%3AAMTDPT%3E2.0.CO%3B2).
- Birk et al. (2021), [*A Revised Bourgouin Precipitation-Type Algorithm*](https://doi.org/10.1175/WAF-D-20-0118.1).
- [MeteoSwiss thesis prototype](https://github.com/MeteoSwiss-APN/precip_diagnostic).
