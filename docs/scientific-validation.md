# Scientific Validation

This repository contains the diagnostic implementation and local synthetic tests.
Operational acceptance still requires a MeteoSwiss-owned validation dataset with
real ICON-CH columns, archived event cases, and observation comparisons. Do not
classify the package as scientifically accepted for production until those
artifacts exist and pass.

## Required Evidence

Before operational use, collect and preserve:

- extracted ICON-CH1-EPS and ICON-CH2-EPS model columns for known precipitation
  type events;
- event metadata: model, date, time, forecast step, member, grid point,
  elevation, nearby station or manual report, and observed precipitation type;
- cases spanning rain, snow, freezing rain, freezing drizzle, ice pellets,
  freezing rain on ground, no precipitation, wet snow or mixed cases, shallow
  cold layers, valley inversions, and high-altitude terrain;
- candidate production GRIB outputs and accepted reference GRIB outputs for
  full-grid regression checks;
- observation CSV files for categorical and any-freezing-precipitation scores.

Keep raw extracted validation data outside normal operational output paths. If
the data cannot be committed because of size or licensing, store it in a stable
internal location and record the exact path, data owner, and extraction command.

## Column Manifest

Use `run_column_validation_manifest()` from `precip_type_diag.verification` to
validate extracted raw columns. The manifest is JSON:

```json
{
  "cases": [
    {
      "name": "example_icon_ch2_freezing_rain_case",
      "temperature_k": [268.4, 270.1, 274.2, 275.0, 271.3],
      "pressure_pa": [72000.0, 78000.0, 84000.0, 91000.0, 98000.0],
      "specific_humidity": [0.0018, 0.0021, 0.0034, 0.0038, 0.0029],
      "full_level_height_m": [3800.0, 2800.0, 1800.0, 800.0, 50.0],
      "total_precip_mm": 0.8,
      "ground_temperature_c": -1.2,
      "expected": "freezing_rain",
      "metadata": {
        "model": "ICON-CH2-EPS",
        "date": "YYYYMMDD",
        "time": "HHMM",
        "step": 12,
        "member": "000",
        "grid_point": "i,j or lon/lat",
        "observation": "station/manual/radar evidence"
      }
    }
  ]
}
```

Run it with Python:

```bash
PYTHONPATH=src python - <<'PY'
from pathlib import Path
import json
from precip_type_diag.verification import run_column_validation_manifest

result = run_column_validation_manifest(Path("validation/icon-column-cases.json"))
print(json.dumps(result, indent=2, sort_keys=True))
raise SystemExit(0 if result["all_passed"] else 1)
PY
```

The manifest runner rejects non-finite values and inconsistent vertical array
lengths. This is intentional: validation cases should be explicit and auditable.

## GRIB Regression Manifest

Use `run_prototype_regression_manifest()` to compare candidate categorical GRIB
outputs to accepted references:

```json
{
  "cases": [
    {
      "name": "icon_ch2_YYYYMMDD_HHMM_member000_step012",
      "candidate_grib": "/path/to/candidate.ptype.grib2",
      "reference_grib": "/path/to/reference.ptype.grib2"
    }
  ]
}
```

The result records shape equality and differing gridpoint counts. Any difference
requires scientific sign-off unless the reference has deliberately changed.

## Observation CSV

Use `load_observation_records_csv()` and `score_observation_records()` for
category-level verification against observations:

```csv
predicted,observed
rain,rain
snow,freezing_rain
12,12
```

The current scorer reports categorical confusion counts and an
any-freezing-precipitation aggregate. For operational acceptance, add agreed
MeteoSwiss thresholds for sample size, high-impact event recall, false-alarm
tolerance, and stratification by region/elevation.

## Acceptance Gate

Minimum suggested gate before production:

- all synthetic and upstream-parity tests pass;
- all column validation cases pass or documented exceptions are approved by a
  domain owner;
- full-grid GRIB regressions match accepted references;
- observation verification meets agreed event-category thresholds;
- the validation dataset version, code commit, dependency environment, and
  command output are archived with the acceptance decision.
