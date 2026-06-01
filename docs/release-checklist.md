# Release Checklist

Use this checklist for release candidates and accepted operational tags.

## Candidate Metadata

- Release candidate:
- Git commit:
- Git tag:
- Package version:
- Python version:
- Realtime FDB `uenv` image:
- ecCodes definition source:
- Release owner:
- Scientific approver:
- Operational approver:

## Local Gates

Run from a clean worktree:

```bash
python -m pip install -e ".[test,dev]"
python -m py_compile src/precip_type_diag/*.py test/*.py
python -m ruff check .
python -m mypy
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m precip_type_diag.benchmark
python -m pip check
```

Expected result: all commands pass. The pytest command enforces the configured
coverage threshold.

## Scientific Validation

- Column validation manifest path:
- GRIB regression manifest path:
- Observation CSV/scoring artifact path:
- Known exceptions:
- Acceptance decision:

Required result: all validation artifacts pass or exceptions are explicitly
approved by the scientific owner.

## Balfrin Smoke Tests

Run one smoke test for each operational model:

```bash
uenv run --view=realtime fdb/5.18:v3 -- \
  env PYTHONPATH=/user-environment/venvs/fdb/lib/python3.11/site-packages:src \
  .venv-fdb/bin/python -m precip_type_diag \
  --model ICON-CH2-EPS \
  --members 000 \
  --max-step 1 \
  --max-wall-s 900 \
  --output-root /users/$USER/work/ptype-fdb-smoke
```

Record:

- CH1 command output:
- CH1 `summary.json`:
- CH1 `monitoring.json`:
- CH2 command output:
- CH2 `summary.json`:
- CH2 `monitoring.json`:

Required result: `monitoring.json["ok"]` is `true`; at least one output GRIB is
re-read and checked for `PTYPE` metadata and allowed category codes.

## Tagging

Tag only after the gates above pass:

```bash
git tag -a vX.Y.Z -m "precip_type_diag vX.Y.Z"
git push origin vX.Y.Z
```

Do not tag accepted operational releases from a dirty worktree unless the exact
diff is archived with the acceptance record.

## Rollback

- Previous accepted tag:
- Previous dependency/uenv record:
- Product publication boundary:
- Rollback command/location:
