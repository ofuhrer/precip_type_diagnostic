# Initial Discovery

This note records the discovery work that preceded the implementation of `precip_type_diag`.

## Cache Layout

The ring cache layout found on `tasna` is:

`/opr/osm/inn/cache/ICON-CH1-EPS/FCST_RING/<run>/icon/<member>/`

`/opr/osm/inn/cache/ICON-CH2-EPS/FCST_RING/<run>/icon/<member>/`

Observed examples on 2026-04-23:

- `ICON-CH1-EPS`: `/opr/osm/inn/cache/ICON-CH1-EPS/FCST_RING/26042315_639/icon`
- `ICON-CH2-EPS`: `/opr/osm/inn/cache/ICON-CH2-EPS/FCST_RING/26042318_741/icon`

Member layout:

- `ICON-CH1-EPS`: `000..010`
- `ICON-CH2-EPS`: `000..020`

File families in each member directory:

- `lfff...` main forecast GRIB
- `lfff...p` pressure levels
- `lfff...z` fixed heights
- `lfff00000000c` constants

Observed forecast-step naming in the current cache:

- `lfff00000000`
- `lfff00010000`
- `lfff00020000`
- ...

The step token uses `DDHHMMSS`, not a plain hour counter. For example:

- `lfff01140000` = `+01 day 14:00` = `+38 h`
- `lfff04180000` = `+04 days 18:00` = `+114 h`

The cache also contains other file families such as `iff...`, `inc...`, `laf...`, and preprocessing artefacts, but they are not required for this first implementation.

## Required Input Fields

The thesis algorithm itself only needs:

- `T`
- `P`
- `QV`
- `HHL`
- `TOT_PREC`
- `T_G`

Additional fields discovered in the cache and MeteoSwiss ecCodes definitions:

- `PS`
- `T_2M`
- `QC`
- `QR`
- `QS`
- `QI`
- `QG`
- `T_S`
- `T_SO`
- `HSURF`

Required MeteoSwiss local shortNames / paramIds:

- `PS=500000`
- `P=500001`
- `HSURF=500007`
- `HHL=500008`
- `T_G=500010`
- `T_2M=500011`
- `T=500014`
- `QV=500035`
- `TOT_PREC=500041`
- `T_S=500061`
- `QC=500100`
- `QI=500101`
- `QR=500102`
- `QS=500103`
- `QG=500106`
- `T_SO=500166`

Raw ecCodes without MeteoSwiss definitions can expose some MeteoSwiss local parameters as generic `h` or `unknown`. Runtime and tests therefore bootstrap ecCodes with MeteoSwiss definitions first and prepend a small project-local overlay afterwards.

## Output Encoding Strategy

Follow-up verification with a real `lssw` template showed that MeteoSwiss already carries a suitable local precipitation-type parameter:

- `paramId=502712`
- `shortName=PTYPE`
- raw GRIB keys: `discipline=0`, `parameterCategory=1`, `parameterNumber=19`

The implemented strategy is intentionally minimal:

- write the existing MeteoSwiss precipitation-type parameter via its raw GRIB keys (`discipline=0`, `parameterCategory=1`, `parameterNumber=19`)
- let ecCodes resolve this to MeteoSwiss `PTYPE` on real `lssw` templates
- extend GRIB2 code table `4.201` locally with one extra category:
  - `13 freezing_rain_on_ground`

The categorical code table used by the implementation is:

- `0 no_precip`
- `1 rain`
- `3 freezing_rain`
- `5 snow`
- `8 ice_pellets`
- `12 freezing_drizzle`
- `13 freezing_rain_on_ground`

This preserves the standard GRIB2 `4.201` values where possible and adds only one local extension for `FZRA_gr`.

## GRIB Metadata Strategy

Output files are written from a copied 2D template message from the input forecast GRIB, preferably the current-hour `TOT_PREC` field. The implementation preserves:

- grid geometry
- analysis/run metadata
- valid time / step metadata
- member metadata

and replaces only:

- parameter metadata
- field values

Each output file contains one categorical integer precipitation-type field per member and forecast hour.
