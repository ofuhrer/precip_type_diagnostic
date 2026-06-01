# Provenance and Licensing Notes

## Repository Code

The package implements an ICON precipitation-type diagnostic following Firdewsa
Zukanovic's MSc thesis method and the upstream prototype listed below. Repository
source code is licensed under the BSD 3-Clause License in `LICENSE`.

## Scientific Method Sources

- Firdewsa Zukanovic MSc thesis: `background/Zukanovic_2023_MScThesis.pdf`
- MeteoSwiss-APN thesis prototype:
  <https://github.com/MeteoSwiss-APN/precip_diagnostic>
- Bourgouin, P. (2000): *A Method to Determine Precipitation Types*,
  `Weather and Forecasting`, 15(5), 583-592.
- Birk, K., E. Lenning, K. Donofrio, and M. T. Friedlein (2021):
  *A Revised Bourgouin Precipitation-Type Algorithm*,
  `Weather and Forecasting`, 36(2), 425-438.

## Bundled Reference Documents

The PDFs in `background/` are reference material for local scientific review.
They are not package data and are not imported by the runtime. Their copyright
and redistribution permissions belong to their original publishers or authors.
Confirm redistribution rights before publishing release archives, wheels,
containers, or public mirrors that include `background/`.

## ecCodes Definitions

`src/precip_type_diag/definitions/` contains the local ecCodes overlay required
to encode the `PTYPE` parameter used by this package. Changes to these files
affect the output GRIB contract and require operational review.

## Release Records

Every accepted release should archive:

- Git tag and commit SHA;
- package version from `pyproject.toml`;
- Python version and dependency versions;
- realtime FDB `uenv` image name/version;
- local MeteoSwiss ecCodes definition source, if overridden;
- validation manifest outputs and observation-scoring summaries;
- Balfrin smoke-test `summary.json` and `monitoring.json`;
- approval owner and date.
