# Consolidated Runtime Path

`precip_type_diag` now uses one production execution path:

- `numba` for categorical diagnosis
- fast single-pass ecCodes GRIB input
- conservative static vertical truncation at `12 km`, derived from `HHL`

These are no longer runtime-selectable options.

Benchmark the current production path on the real CH2 fixture:

```bash
PYTHONPATH=src python -m precip_type_diag.benchmark --case real-ch2
```

Benchmark the current production path on synthetic columns:

```bash
PYTHONPATH=src python -m precip_type_diag.benchmark --case synthetic
```
