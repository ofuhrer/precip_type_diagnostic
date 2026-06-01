# Balfrin FDB Smoke Evidence, 2026-05-31 Runs

Smoke tests were run on Balfrin from the synced working tree at:

```text
/users/olifu/work/ptype-codex-smoke/precip_type_diagnostic
```

The synced tree was based on local Git commit
`b07678508cde53bc1e9885e0bd971d0577b8f876` plus the uncommitted monitoring,
profile-extraction, release, and maintenance-hardening changes in this branch.
Because the tree was synced without `.git`, the archived runtime summaries report
`provenance.git.available=false`. For a formal accepted release, rerun this smoke
from the annotated release tag so summaries contain Git provenance.

Runtime environment:

- host: `balfrin-ln003`
- uenv: `fdb/5.18:v3`
- Python: `3.11.6`
- FDB site-packages:
  `/user-environment/venvs/fdb/lib/python3.11/site-packages`
- additional venv for missing runtime dependency:
  `/users/olifu/work/ptype-codex-smoke/.venv-fdb-smoke`

The FDB uenv provided Earthkit, ecCodes, NumPy, and FDB access, but did not
provide `numba`. The smoke run therefore used the FDB site-packages first on
`PYTHONPATH` and a small venv for `numba`.

Commands:

```bash
uenv run --view=realtime fdb/5.18:v3 -- bash /users/olifu/work/ptype-codex-smoke/run_smokes.sh
uenv run --view=realtime fdb/5.18:v3 -- bash -lc '
  export PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:/users/olifu/work/ptype-codex-smoke/precip_type_diagnostic/src
  /users/olifu/work/ptype-codex-smoke/.venv-fdb-smoke/bin/python /users/olifu/work/ptype-codex-smoke/validate_artifacts.py
'
```

Results:

- `ICON-CH1-EPS`, member `000`, `max_step=1`, run `20260531 2100`:
  `monitoring.ok=true`, `wall_s=12.875`.
- `ICON-CH2-EPS`, member `000`, `max_step=1`, run `20260531 1800`:
  `monitoring.ok=true`, `wall_s=8.086`.
- Re-read GRIB validation passed for step 0 and step 1 outputs for both models.
- Output GRIBs had `shortName=PTYPE`, `paramId=502712`, and only allowed
  category codes.

Archived files:

- `ch1-command-output.json`, `ch1-summary.json`, `ch1-monitoring.json`,
  `ch1-stderr.log`
- `ch2-command-output.json`, `ch2-summary.json`, `ch2-monitoring.json`,
  `ch2-stderr.log`
- `grib-validation.json`
- `environment.json`

The output GRIB files themselves are not committed. They remain on Balfrin under:

```text
/users/olifu/work/ptype-codex-smoke/output
```
