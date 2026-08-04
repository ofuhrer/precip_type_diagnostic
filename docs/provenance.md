# Provenance and Licensing Notes

## Repository Code

The package implements an ICON precipitation-type diagnostic following Firdewsa
Zukanovic's MSc thesis method and the upstream prototype listed below. Repository
source code is licensed under the BSD 3-Clause License in `LICENSE`.

## Scientific Method Sources

- Firdewsa Zukanovic MSc thesis:
  [local PDF](../background/Zukanovic_2023_MScThesis.pdf)
- MeteoSwiss-APN thesis prototype:
  <https://github.com/MeteoSwiss-APN/precip_diagnostic>
- ICON online diagnostic source, commit
  [`50da7c5924994f7626688eb5185b8e66c781b12e`](https://gitlab.dkrz.de/icon/icon-nwp/-/commit/50da7c5924994f7626688eb5185b8e66c781b12e),
  particularly `src/atm_phy_nwp/mo_diag_precip_type.f90`. The offline ICON mode
  and its executable comparison harness are pinned to this revision.
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

The bundled documents are:

- [Zukanovic MSc thesis](../background/Zukanovic_2023_MScThesis.pdf)
- [Bourgouin (2000)](../background/Bourgouin_2000_WeatherAndForecasting.pdf)
- [Birk et al. (2021)](../background/BirkEtAl_2021_WeatherAndForecasting.pdf)
- [Benjamin et al. (2016)](../background/BenjaminEtAl_2016_WeatherAndForecasting.pdf)

The scientific method sources above are the authoritative citations for this
implementation.

`test/data/icon_fortran_reference.json` is generated output, not copied source
code. It records deterministic input columns, categorical results produced by
the pinned Fortran module, and the SHA-256 of that module. The verifier extracts
the module from a separate ICON checkout at runtime; the ICON Fortran source is
not redistributed in this repository.

## ecCodes Definitions

`src/precip_type_diag/definitions/` contains the local ecCodes overlay required
to encode the `PTYPE` parameter used by this package. Changes to these files
affect GRIB2 member output encoding and require operational review.

## Release Records

Every accepted release should archive:

- Git tag and commit SHA;
- package version from `pyproject.toml`;
- Python version and dependency versions;
- FDB `uenv` image name/version and tested `realtime` / `rea-l-ch1` views;
- local MeteoSwiss ecCodes definition source, if overridden;
- Balfrin smoke-test `summary.json` and `monitoring.json` for each supported
  source covered by the release;
- approval owner and date.
