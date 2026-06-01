# Scientific Validation Candidate Profiles

This directory contains diagnostic-selected ICON-CH profile candidates gathered
from realtime FDB on Balfrin. These files are triage material for scientific
review, not accepted validation truth.

## ICON-CH1-EPS 20260531 2100 Member 000

`icon-ch1-20260531-2100-member000-profile-candidates.json` contains 36 column
profiles from `ICON-CH1-EPS` member `000` at forecast steps 1, 3, 6, and 12.
The extraction requested rain, snow, freezing drizzle, freezing rain, and ice
pellets, with up to three samples per category and step. This FDB cycle and step
set contained:

- 12 rain candidates;
- 12 snow candidates;
- 12 freezing-drizzle candidates;
- no freezing-rain or ice-pellet candidates among the selected samples.

The profiles were extracted on Balfrin with:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:/users/$USER/work/ptype-codex-science/precip_type_diagnostic/src \
  /users/$USER/work/ptype-codex-science/.venv-fdb-science/bin/python \
  -m precip_type_diag.profile_samples \
  --model ICON-CH1-EPS \
  --member 000 \
  --date 20260531 \
  --time 2100 \
  --steps STEP \
  --select-diagnostic-types rain,snow,freezing_drizzle,freezing_rain,ice_pellets \
  --samples-per-type 3 \
  --output icon-ch1-step${STEP}-profile-candidates.json
```

The extraction was run once for each `STEP` in `1 3 6 12`, then merged into the
committed JSON file.

Environment observed during extraction:

- `uenv`: `fdb/5.18:v3`, `realtime` view;
- `eccodes`: 2.39.2;
- `earthkit.data`: 0.18.3;
- `numpy`: 1.26.4 from the FDB view;
- `numba`: 0.65.1 from the side virtual environment.

Before these candidates can support scientific acceptance, match each retained
case to independent station, manual, radar, or nowcast evidence and add the
accepted `expected` label expected by
`precip_type_diag.verification.run_column_validation_manifest()`.
