# Consolidated Runtime Path

`precip_type_diag` now uses one production execution path:

- `numba` for categorical diagnosis
- indexed ecCodes GRIB input with a sequential scanner fallback
- conservative static vertical truncation at `12 km`, derived from `HHL`

These are no longer runtime-selectable options.

GRIB input uses ecCodes parameter indexes to retrieve only the messages required
by the diagnostic. Small index files are cached under the directory named by
`PRECIP_TYPE_DIAG_GRIB_INDEX_CACHE`, or under the system temporary directory by
default. Set `PRECIP_TYPE_DIAG_GRIB_INDEX_CACHE=off` to disable the cache and
build indexes in memory for each read.

Benchmark the current production path on the real CH2 fixture:

```bash
PYTHONPATH=src python -m precip_type_diag.benchmark --case real-ch2
```

Benchmark the current production path on synthetic columns:

```bash
PYTHONPATH=src python -m precip_type_diag.benchmark --case synthetic
```
